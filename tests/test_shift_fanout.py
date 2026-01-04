import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient

import app.api as api
from app.api import create_app
from app.database import InMemoryKeyValueDatabase
from app.models import Caregiver, Shift


def _p(msg: str) -> None:
    # pytest captures stdout unless you run with -s
    print(msg, flush=True)


def _banner(name: str) -> None:
    _p("\n" + "=" * 88)
    _p(f"test: {name}")
    _p("=" * 88)


def _dump_db(app, *, shift_id: str | None = None) -> None:
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = app.state.database
    caregivers: list[Caregiver] = [
        c for c in db.all() if isinstance(c, Caregiver)
    ]
    shifts: list[Shift] = [s for s in db.all() if isinstance(s, Shift)]

    _p("db caregivers:")
    for c in sorted(caregivers, key=lambda x: x.id):
        _p(f"  - {c.id} | {c.name} | role={c.role} | phone={c.phone}")

    _p("db shifts:")
    for s in sorted(shifts, key=lambda x: x.id):
        _p(
            f"  - {s.id} | role_required={s.role_required} | "
            f"claimed={s.claimed} claimed_by={s.claimed_by} "
            f"fanout_started_at={s.fanout_started_at} "
            f"declined={list(s.declined_caregiver_ids)}"
        )

    if shift_id is not None:
        s = db.get(f"shift:{shift_id}")
        if isinstance(s, Shift):
            matching = [
                c
                for c in caregivers
                if c.role == s.role_required
                and c.id not in s.declined_caregiver_ids
            ]
            _p(f"computed matching caregivers for shift {shift_id}:")
            for c in matching:
                _p(f"  - {c.id} ({c.role}) {c.phone}")
        else:
            _p(f"shift:{shift_id} not found in db")


class FreezegunSleeper:
    """
    Fake sleep function that advances only when the test manually ticks
    freezegun time forward, to simulate 10 minute wait.
    """

    def __init__(self, frozen_time):
        self.frozen_time = frozen_time
        self._event = asyncio.Event()

    def tick(self, *, delta: timedelta) -> None:
        before = datetime.now(UTC)
        self.frozen_time.tick(delta=delta)
        after = datetime.now(UTC)
        _p(
            f"[time] ticked by {delta}. {before.isoformat()} -> {after.isoformat()}"
        )
        self._event.set()

    async def sleep(self, seconds: float) -> None:
        start = datetime.now(UTC)
        deadline = start + timedelta(seconds=seconds)
        _p(
            f"[sleep] requested {seconds:.2f}s from {start.isoformat()} until {deadline.isoformat()}"
        )

        while datetime.now(UTC) < deadline:
            await self._event.wait()
            self._event.clear()

        _p(f"[sleep] done at {datetime.now(UTC).isoformat()}")


@pytest.fixture(autouse=True)
def notifier_mocks(monkeypatch):
    """
    Patch api-level imports (api.py does `from app.notifier import ...`),
    so patching app.notifier.* would not affect the app.
    """
    sms = AsyncMock(return_value=None)
    call = AsyncMock(return_value=None)
    monkeypatch.setattr(api, "send_sms", sms)
    monkeypatch.setattr(api, "place_phone_call", call)
    return sms, call


@pytest_asyncio.fixture
async def client():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client

    # cancel any pending escalation tasks
    tasks = []
    if hasattr(app.state, "escalation_tasks"):
        tasks = list(app.state.escalation_tasks)

    for t in tasks:
        t.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest_asyncio.fixture
async def setup_test_data(client: AsyncClient):
    app = client._transport.app
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = app.state.database

    alice = Caregiver(
        id="alice-id",
        name="Alice Ongwele",
        role="RN",
        phone="+15550001",
    )
    wei = Caregiver(
        id="wei-id",
        name="Wei Yan",
        role="LPN",
        phone="+15550002",
    )
    barry = Caregiver(
        id="barry-id",
        name="Barry Kozumikov",
        role="LPN",
        phone="+15550003",
    )

    rn_shift = Shift(
        id="rn-shift-123",
        organization_id="org-123",
        role_required="RN",
        start_time=datetime(2025, 7, 2, 8, 0, 0, tzinfo=UTC),
        end_time=datetime(2025, 7, 2, 16, 0, 0, tzinfo=UTC),
    )
    lpn_shift = Shift(
        id="lpn-shift-456",
        organization_id="org-123",
        role_required="LPN",
        start_time=datetime(2025, 7, 2, 16, 0, 0, tzinfo=UTC),
        end_time=datetime(2025, 7, 3, 0, 0, 0, tzinfo=UTC),
    )

    db.put(f"caregiver:{alice.id}", alice)
    db.put(f"caregiver:{wei.id}", wei)
    db.put(f"caregiver:{barry.id}", barry)
    db.put(f"shift:{rn_shift.id}", rn_shift)
    db.put(f"shift:{lpn_shift.id}", lpn_shift)


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient) -> None:
    _banner("health_check returns ok")
    resp = await client.get("/health")
    _p(f"GET /health -> status={resp.status_code}, body={resp.json()}")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_fanout_shift_not_found(client: AsyncClient) -> None:
    _banner("fanout_shift returns 404 for missing shift")
    resp = await client.post("/shifts/nonexistent/fanout")
    _p(
        f"POST /shifts/nonexistent/fanout -> status={resp.status_code}, body={resp.json()}"
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_fanout_filters_by_role_for_sms(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner("fanout sends sms only to caregivers matching role_required")
    sms_mock, call_mock = notifier_mocks
    app = client._transport.app
    _dump_db(app, shift_id="rn-shift-123")

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        _p(f"[time] now={datetime.now(UTC).isoformat()}")
        resp = await client.post("/shifts/rn-shift-123/fanout")
        _p(
            f"POST /shifts/rn-shift-123/fanout -> status={resp.status_code}, body={resp.json()}"
        )

        _p(f"sms calls made: {sms_mock.await_count}")
        if sms_mock.await_count:
            for i, (args, _kw) in enumerate(sms_mock.await_args_list, start=1):
                phone, msg = args
                _p(f"  sms[{i}] -> phone={phone}, msg='{msg}'")

        _p(
            f"phone calls made (should be 0 before tick): {call_mock.await_count}"
        )

        assert resp.status_code == 200

        # sms only to RN (alice)
        assert sms_mock.await_count == 1
        (phone, message), _ = sms_mock.await_args
        assert phone == "+15550001"
        assert "rn-shift-123" in message.lower()

        # should NOT have escalated yet (we didn't tick 10 minutes)
        assert call_mock.await_count == 0


@pytest.mark.asyncio
async def test_fanout_contacts_all_matching_role_for_sms(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner("fanout contacts all caregivers with matching role_required")
    sms_mock, _ = notifier_mocks
    app = client._transport.app
    _dump_db(app, shift_id="lpn-shift-456")

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        resp = await client.post("/shifts/lpn-shift-456/fanout")
        _p(
            f"POST /shifts/lpn-shift-456/fanout -> status={resp.status_code}, body={resp.json()}"
        )

        _p(f"sms calls made: {sms_mock.await_count}")
        for i, (args, _kw) in enumerate(sms_mock.await_args_list, start=1):
            phone, msg = args
            _p(f"  sms[{i}] -> phone={phone}, msg='{msg}'")

        assert resp.status_code == 200
        assert sms_mock.await_count == 2
        phones = sorted([args[0] for (args, _) in sms_mock.await_args_list])
        assert phones == ["+15550002", "+15550003"]


@pytest.mark.asyncio
async def test_fanout_idempotent_no_duplicate_sms(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner(
        "fanout is idempotent: no duplicate sms and no duplicate escalation task"
    )
    sms_mock, _ = notifier_mocks
    app = client._transport.app

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        r1 = await client.post("/shifts/rn-shift-123/fanout")
        _p(f"first fanout -> status={r1.status_code}, body={r1.json()}")
        _p(f"sms await_count after first fanout: {sms_mock.await_count}")
        _p(
            f"escalation tasks count after first fanout: {len(app.state.escalation_tasks)}"
        )

        r2 = await client.post("/shifts/rn-shift-123/fanout")
        _p(f"second fanout -> status={r2.status_code}, body={r2.json()}")
        _p(f"sms await_count after second fanout: {sms_mock.await_count}")
        _p(
            f"escalation tasks count after second fanout: {len(app.state.escalation_tasks)}"
        )

        assert r1.status_code == 200
        assert sms_mock.await_count == 1
        assert len(app.state.escalation_tasks) == 1

        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["status"] == "already_fanout"
        assert sms_mock.await_count == 1
        assert len(app.state.escalation_tasks) == 1


@pytest.mark.asyncio
async def test_inbound_message_caregiver_not_found(
    client: AsyncClient, setup_test_data
) -> None:
    _banner("inbound accept fails if caregiver phone is unknown")
    resp = await client.post(
        "/messages/inbound",
        json={"from": "+15559999", "body": "yes", "shift_id": "rn-shift-123"},
    )
    _p(
        f"POST /messages/inbound (unknown phone) -> status={resp.status_code}, body={resp.json()}"
    )
    assert resp.status_code == 404
    assert "caregiver" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_inbound_message_shift_not_found(
    client: AsyncClient, setup_test_data
) -> None:
    _banner("inbound accept fails if shift is unknown")
    resp = await client.post(
        "/messages/inbound",
        json={"from": "+15550001", "body": "yes", "shift_id": "nope"},
    )
    _p(
        f"POST /messages/inbound (unknown shift) -> status={resp.status_code}, body={resp.json()}"
    )
    assert resp.status_code == 404
    assert "shift" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_accept_claims_shift_and_sets_fields(
    client: AsyncClient, setup_test_data
) -> None:
    _banner("accept claims shift + sets claimed fields in db")
    app = client._transport.app
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = app.state.database

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        _p("triggering fanout first...")
        await client.post("/shifts/rn-shift-123/fanout")
        _dump_db(app, shift_id="rn-shift-123")

        _p("alice replies YES to accept")
        resp = await client.post(
            "/messages/inbound",
            json={
                "from": "+15550001",
                "body": "yes",
                "shift_id": "rn-shift-123",
            },
        )
        _p(f"inbound accept -> status={resp.status_code}, body={resp.json()}")

        shift = db.get("shift:rn-shift-123")
        assert isinstance(shift, Shift)
        _p(
            "db shift after accept:\n"
            f"  claimed={shift.claimed} claimed_by={shift.claimed_by} claimed_at={shift.claimed_at}"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "claimed"
        assert data["caregiver_id"] == "alice-id"
        assert shift.claimed is True
        assert shift.claimed_by == "alice-id"
        assert shift.claimed_at is not None


@pytest.mark.asyncio
async def test_only_one_caregiver_can_claim_even_if_two_accept(
    client: AsyncClient, setup_test_data
) -> None:
    _banner("race: two caregivers accept at same time -> only one wins")
    app = client._transport.app
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = app.state.database

    eve = Caregiver(
        id="eve-id",
        name="Eve Example",
        role="RN",
        phone="+15550004",
    )
    db.put(f"caregiver:{eve.id}", eve)
    _p("added second RN: eve")
    _dump_db(app, shift_id="rn-shift-123")

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        _p("triggering fanout...")
        await client.post("/shifts/rn-shift-123/fanout")

        _p("simultaneous accepts: alice + eve")
        r1, r2 = await asyncio.gather(
            client.post(
                "/messages/inbound",
                json={
                    "from": "+15550001",
                    "body": "yes",
                    "shift_id": "rn-shift-123",
                },
            ),
            client.post(
                "/messages/inbound",
                json={
                    "from": "+15550004",
                    "body": "yes",
                    "shift_id": "rn-shift-123",
                },
            ),
        )
        _p(f"alice response: {r1.json()}")
        _p(f"eve response:   {r2.json()}")

        statuses = sorted([r1.json()["status"], r2.json()["status"]])
        _p(f"statuses: {statuses} (expect one claimed, one already_claimed)")
        assert statuses == ["already_claimed", "claimed"]

        shift = db.get("shift:rn-shift-123")
        assert isinstance(shift, Shift)
        _p(f"winner in db: claimed_by={shift.claimed_by}")
        assert shift.claimed is True
        assert shift.claimed_by in {"alice-id", "eve-id"}


@pytest.mark.asyncio
async def test_decline_is_tracked_on_shift(
    client: AsyncClient, setup_test_data
) -> None:
    _banner("decline is tracked on the shift (declined_caregiver_ids)")
    app = client._transport.app
    db: InMemoryKeyValueDatabase[str, Shift | Caregiver] = app.state.database

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True):
        await client.post("/shifts/lpn-shift-456/fanout")
        _dump_db(app, shift_id="lpn-shift-456")

        _p("wei replies NO to decline")
        resp = await client.post(
            "/messages/inbound",
            json={
                "from": "+15550002",
                "body": "no",
                "shift_id": "lpn-shift-456",
            },
        )
        _p(f"inbound decline -> status={resp.status_code}, body={resp.json()}")

        shift = db.get("shift:lpn-shift-456")
        assert isinstance(shift, Shift)
        _p(
            f"db declined_caregiver_ids now: {list(shift.declined_caregiver_ids)}"
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "not_claimed"
        assert "wei-id" in shift.declined_caregiver_ids


@pytest.mark.asyncio
async def test_escalation_waits_full_10_minutes_then_calls(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner("escalation waits 10 minutes, then places phone calls")
    _, call_mock = notifier_mocks
    app = client._transport.app

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True) as frozen:
        sleeper = FreezegunSleeper(frozen)
        app.state.now_fn = lambda: datetime.now(UTC)
        app.state.sleep_fn = sleeper.sleep

        _p(
            "triggering fanout for LPN shift (should set up escalation background task)"
        )
        await client.post("/shifts/lpn-shift-456/fanout")
        _dump_db(app, shift_id="lpn-shift-456")

        # let background task start and enter sleep()
        await asyncio.sleep(0)
        _p(f"call await_count immediately: {call_mock.await_count}")

        _p("advance time by 9 minutes (should still be no calls)")
        sleeper.tick(delta=timedelta(minutes=9))
        await asyncio.sleep(0)
        _p(f"call await_count at +9m: {call_mock.await_count}")
        assert call_mock.await_count == 0

        _p("advance time by 1 more minute (hit +10m => should call both LPNs)")
        sleeper.tick(delta=timedelta(minutes=1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # let gather complete

        _p(f"call await_count at +10m: {call_mock.await_count}")
        for i, (args, _kw) in enumerate(call_mock.await_args_list, start=1):
            phone, msg = args
            _p(f"  call[{i}] -> phone={phone}, msg='{msg}'")

        assert call_mock.await_count == 2
        phones = sorted([args[0] for (args, _) in call_mock.await_args_list])
        assert phones == ["+15550002", "+15550003"]


@pytest.mark.asyncio
async def test_escalation_excludes_declined_caregivers(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner("escalation excludes caregivers who declined before 10 minutes")
    _, call_mock = notifier_mocks
    app = client._transport.app

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True) as frozen:
        sleeper = FreezegunSleeper(frozen)
        app.state.now_fn = lambda: datetime.now(UTC)
        app.state.sleep_fn = sleeper.sleep

        await client.post("/shifts/lpn-shift-456/fanout")
        await asyncio.sleep(0)

        _p("wei declines before the 10-minute mark")
        await client.post(
            "/messages/inbound",
            json={
                "from": "+15550002",
                "body": "no",
                "shift_id": "lpn-shift-456",
            },
        )
        _dump_db(app, shift_id="lpn-shift-456")

        _p("advance time by +10m (should call only barry)")
        sleeper.tick(delta=timedelta(minutes=10))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        _p(f"call await_count: {call_mock.await_count}")
        for i, (args, _kw) in enumerate(call_mock.await_args_list, start=1):
            phone, msg = args
            _p(f"  call[{i}] -> phone={phone}, msg='{msg}'")

        assert call_mock.await_count == 1
        (phone, _msg), _ = call_mock.await_args
        assert phone == "+15550003"


@pytest.mark.asyncio
async def test_escalation_is_cancelled_when_shift_is_claimed(
    client: AsyncClient, setup_test_data, notifier_mocks
) -> None:
    _banner("escalation is cancelled when shift gets claimed before 10 minutes")
    _, call_mock = notifier_mocks
    app = client._transport.app

    with freeze_time("2025-07-02 00:00:00", real_asyncio=True) as frozen:
        sleeper = FreezegunSleeper(frozen)
        app.state.now_fn = lambda: datetime.now(UTC)
        app.state.sleep_fn = sleeper.sleep

        await client.post("/shifts/lpn-shift-456/fanout")
        await asyncio.sleep(0)

        _p("advance time +5m, then claim shift")
        sleeper.tick(delta=timedelta(minutes=5))
        await asyncio.sleep(0)

        await client.post(
            "/messages/inbound",
            json={
                "from": "+15550002",
                "body": "yes",
                "shift_id": "lpn-shift-456",
            },
        )
        _dump_db(app, shift_id="lpn-shift-456")

        _p(
            "advance time another +5m (reach +10m). should still be 0 phone calls."
        )
        sleeper.tick(delta=timedelta(minutes=5))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        _p(f"call await_count: {call_mock.await_count}")
        assert call_mock.await_count == 0

        task = app.state.escalation_tasks_by_shift.get("lpn-shift-456")
        _p(f"escalation task present? {task is not None}")
        if task is not None:
            _p(f"task state: done={task.done()} cancelled={task.cancelled()}")
            assert task.cancelled() or task.done()
