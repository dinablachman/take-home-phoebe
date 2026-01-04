import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import InMemoryKeyValueDatabase
from app.intent import (
    ShiftRequestMessageIntent,
    parse_shift_request_message_intent,
)
from app.models import Caregiver, Shift
from app.notifier import send_sms

router = APIRouter()


class InboundMessageRequest(BaseModel):
    from_: str = Field(alias="from")  # Phone number
    body: str  # Message text
    shift_id: str  # Shift identifier


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/shifts/{shift_id}/fanout")
async def fanout_shift(shift_id: str, request: Request) -> dict:
    """
    Trigger fanout for a shift.
    Round 1: Send SMS to qualifying caregivers.
    Idempotent: re-posting will not send duplicate notifications.
    """
    # Get database from app state
    database: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        request.app.state.database
    )

    # Get shift from database
    shift = database.get(f"shift:{shift_id}")
    if not shift or not isinstance(shift, Shift):
        raise HTTPException(status_code=404, detail="Shift not found")

    # Idempotency check: if fanout already started, return early
    if shift.fanout_started_at is not None:
        return {
            "shift_id": shift_id,
            "status": "already_fanout",
            "fanout_started_at": shift.fanout_started_at.isoformat(),
        }

    # Filter caregivers by role_required
    all_caregivers = database.all()
    qualifying_caregivers = [
        c
        for c in all_caregivers
        if isinstance(c, Caregiver) and c.role == shift.role_required
    ]

    # Set fanout_started_at timestamp
    shift.fanout_started_at = datetime.now(UTC)
    database.put(f"shift:{shift_id}", shift)

    # Send SMS to all qualifying caregivers concurrently
    # Include shift_id in message for response handling
    message = f"Shift {shift_id} available. Reply 'yes' to accept."
    sms_tasks = [
        send_sms(caregiver.phone, message)
        for caregiver in qualifying_caregivers
    ]
    await asyncio.gather(*sms_tasks)

    # Start escalation timer (Round 2: phone calls after 10 minutes)
    escalation_task = asyncio.create_task(
        escalate_if_unfilled(shift_id, request.app.state.database)
    )
    # Task runs in background, we don't need to await it

    return {
        "shift_id": shift_id,
        "role_required": shift.role_required,
        "qualifying_caregivers": len(qualifying_caregivers),
        "fanout_started_at": shift.fanout_started_at.isoformat(),
    }


@router.post("/messages/inbound")
async def handle_inbound_message(
    message: InboundMessageRequest, request: Request
) -> dict:
    """
    Handle incoming SMS/phone message from caregiver.
    """
    # Get database from app state
    database: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        request.app.state.database
    )

    # Find caregiver by phone number
    all_caregivers = database.all()
    caregiver = next(
        (
            c
            for c in all_caregivers
            if isinstance(c, Caregiver) and c.phone == message.from_
        ),
        None,
    )
    if not caregiver:
        raise HTTPException(
            status_code=404, detail="Caregiver not found for phone number"
        )

    # Get shift from database
    shift = database.get(f"shift:{message.shift_id}")
    if not shift or not isinstance(shift, Shift):
        raise HTTPException(status_code=404, detail="Shift not found")

    # Parse message intent
    intent = await parse_shift_request_message_intent(message.body)

    # Handle ACCEPT intent
    if intent == ShiftRequestMessageIntent.ACCEPT:
        # Re-fetch shift from database to get latest state
        current_shift = database.get(f"shift:{message.shift_id}")
        if not current_shift or not isinstance(current_shift, Shift):
            raise HTTPException(status_code=404, detail="Shift not found")

        # Check if already claimed, using latest state from database
        if current_shift.claimed:
            return {
                "status": "already_claimed",
                "shift_id": message.shift_id,
                "message": "Shift has already been claimed by another caregiver",
            }

        # Claim the shift
        current_shift.claimed = True
        current_shift.claimed_by = caregiver.id
        current_shift.claimed_at = datetime.now(UTC)
        database.put(f"shift:{message.shift_id}", current_shift)

        return {
            "status": "claimed",
            "shift_id": message.shift_id,
            "caregiver_id": caregiver.id,
            "claimed_at": current_shift.claimed_at.isoformat(),
        }

    # Handle DECLINE intent - track declined caregiver
    if intent == ShiftRequestMessageIntent.DECLINE:
        # Re-fetch shift from database to get latest state
        current_shift = database.get(f"shift:{message.shift_id}")
        if not current_shift or not isinstance(current_shift, Shift):
            raise HTTPException(status_code=404, detail="Shift not found")

        if caregiver.id not in current_shift.declined_caregiver_ids:
            current_shift.declined_caregiver_ids.append(caregiver.id)
            database.put(f"shift:{message.shift_id}", current_shift)

    # Handle DECLINE or UNKNOWN intents
    return {
        "status": "not_claimed",
        "shift_id": message.shift_id,
        "intent": intent.value,
    }


async def escalate_if_unfilled(
    shift_id: str, database: InMemoryKeyValueDatabase[str, Shift | Caregiver]
) -> None:
    """
    Background task that escalates to phone calls if shift is not claimed
    within 10 minutes. Uses time comparison (freezegun-friendly).
    """
    # Get shift to find when fanout started
    shift = database.get(f"shift:{shift_id}")
    if not shift or not isinstance(shift, Shift):
        return  # Shift doesn't exist

    if shift.fanout_started_at is None:
        return  # Fanout never started

    # Normalize fanout_started_at to UTC-aware datetime
    fanout_started_at = shift.fanout_started_at
    if fanout_started_at.tzinfo is None:
        # Assume naive datetime is UTC
        fanout_started_at = fanout_started_at.replace(tzinfo=UTC)

    # Calculate target time (10 minutes after fanout started)
    target_time = fanout_started_at + timedelta(minutes=10)

    # Wait until target time, checking periodically
    while True:
        current_time = datetime.now(UTC)

        # Check if shift was claimed (exit early if claimed)
        shift = database.get(f"shift:{shift_id}")
        if not shift or not isinstance(shift, Shift):
            return  # Shift deleted
        if shift.claimed:
            return  # Shift was claimed, no escalation needed

        # Check if 10 minutes have passed
        if current_time >= target_time:
            # TODO: Send phone calls to qualifying caregivers
            # (excluding those who declined)
            break

        # Small sleep before checking again
        await asyncio.sleep(0.1)


def create_app() -> FastAPI:
    app = FastAPI()

    # Initialize database
    database: InMemoryKeyValueDatabase[str, Shift | Caregiver] = (
        InMemoryKeyValueDatabase()
    )
    app.state.database = database

    app.include_router(router)
    return app
