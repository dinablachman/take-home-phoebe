import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import InMemoryKeyValueDatabase
from app.intent import (
    ShiftRequestMessageIntent,
    parse_shift_request_message_intent,
)
from app.models import Caregiver, Shift
from app.notifier import place_phone_call, send_sms

router = APIRouter()

NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]


class InboundMessageRequest(BaseModel):
    from_: str = Field(alias="from")
    body: str
    shift_id: str


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/shifts/{shift_id}/fanout")
async def fanout_shift(shift_id: str, request: Request) -> dict:
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        request.app.state.database
    )

    shift = db.get(f"shift:{shift_id}")
    if not shift or not isinstance(shift, Shift):
        raise HTTPException(status_code=404, detail="Shift not found")

    # idempotency: set before any awaits so concurrent calls can’t interleave here
    if shift.fanout_started_at is not None:
        return {
            "shift_id": shift_id,
            "status": "already_fanout",
            "fanout_started_at": shift.fanout_started_at.isoformat(),
        }

    shift.fanout_started_at = request.app.state.now_fn()
    db.put(f"shift:{shift_id}", shift)

    caregivers = [
        c
        for c in db.all()
        if isinstance(c, Caregiver) and c.role == shift.role_required
    ]

    message = f"Shift {shift_id} available. Reply 'yes' to accept."
    await asyncio.gather(*(send_sms(c.phone, message) for c in caregivers))

    task = asyncio.create_task(
        escalate_if_unfilled(
            shift_id,
            db,
            now_fn=request.app.state.now_fn,
            sleep_fn=request.app.state.sleep_fn,
        )
    )
    request.app.state.escalation_tasks.add(task)
    request.app.state.escalation_tasks_by_shift[shift_id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        request.app.state.escalation_tasks.discard(_t)
        request.app.state.escalation_tasks_by_shift.pop(shift_id, None)

    task.add_done_callback(_cleanup)

    return {
        "shift_id": shift_id,
        "role_required": shift.role_required,
        "qualifying_caregivers": len(caregivers),
        "fanout_started_at": shift.fanout_started_at.isoformat(),
    }


@router.post("/messages/inbound")
async def handle_inbound_message(
    message: InboundMessageRequest, request: Request
) -> dict:
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        request.app.state.database
    )

    caregiver = next(
        (
            c
            for c in db.all()
            if isinstance(c, Caregiver) and c.phone == message.from_
        ),
        None,
    )
    if not caregiver:
        raise HTTPException(
            status_code=404, detail="Caregiver not found for phone number"
        )

    shift = db.get(f"shift:{message.shift_id}")
    if not shift or not isinstance(shift, Shift):
        raise HTTPException(status_code=404, detail="Shift not found")

    intent = await parse_shift_request_message_intent(message.body)

    if intent == ShiftRequestMessageIntent.ACCEPT:
        claimed_at = request.app.state.now_fn()
        claimed = db.claim_shift_if_unclaimed(
            f"shift:{message.shift_id}", caregiver.id, claimed_at
        )

        if not claimed:
            return {
                "status": "already_claimed",
                "shift_id": message.shift_id,
                "message": "Shift has already been claimed by another caregiver",
            }

        # cancel escalation (if sleeping)
        task = request.app.state.escalation_tasks_by_shift.get(message.shift_id)
        if task is not None:
            task.cancel()

        return {
            "status": "claimed",
            "shift_id": message.shift_id,
            "caregiver_id": caregiver.id,
            "claimed_at": claimed_at.isoformat(),
        }

    if intent == ShiftRequestMessageIntent.DECLINE:
        if caregiver.id not in shift.declined_caregiver_ids:
            shift.declined_caregiver_ids.append(caregiver.id)
            db.put(f"shift:{message.shift_id}", shift)

    return {
        "status": "not_claimed",
        "shift_id": message.shift_id,
        "intent": intent.value,
    }


async def escalate_if_unfilled(
    shift_id: str,
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver],
    *,
    now_fn: NowFn,
    sleep_fn: SleepFn,
) -> None:
    shift = db.get(f"shift:{shift_id}")
    if (
        not shift
        or not isinstance(shift, Shift)
        or shift.fanout_started_at is None
    ):
        return

    start = shift.fanout_started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    target = start + timedelta(minutes=10)

    try:
        remaining = (target - now_fn()).total_seconds()
        if remaining > 0:
            await sleep_fn(remaining)

        shift = db.get(f"shift:{shift_id}")
        if not shift or not isinstance(shift, Shift) or shift.claimed:
            return

        caregivers = [
            c
            for c in db.all()
            if isinstance(c, Caregiver)
            and c.role == shift.role_required
            and c.id not in shift.declined_caregiver_ids
        ]

        message = f"Shift {shift_id} available. Reply 'yes' to accept."
        await asyncio.gather(
            *(place_phone_call(c.phone, message) for c in caregivers)
        )

    except asyncio.CancelledError:
        return


def create_app() -> FastAPI:
    app = FastAPI()
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        InMemoryKeyValueDatabase()
    )
    app.state.database = db

    app.state.now_fn = lambda: datetime.now(UTC)
    app.state.sleep_fn = asyncio.sleep

    app.state.escalation_tasks = set()
    app.state.escalation_tasks_by_shift = {}

    app.include_router(router)
    return app
