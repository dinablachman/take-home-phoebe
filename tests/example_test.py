from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient

from app.api import create_app


@pytest_asyncio.fixture
async def client():
    """
    Test fixture that creates an async client for the API.
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient) -> None:
    """
    Example test that uses the api client fixture to test the health check
    endpoint.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_mocked_time(client: AsyncClient) -> None:
    """
    Example test that shows how to mock time, including freezing time at a
    deterministic point and manually ticking time forward.
    """
    with freeze_time("2025-07-02 00:00:00") as frozen_time:
        assert datetime.now(UTC) == datetime(2025, 7, 2, 0, 0, 0, tzinfo=UTC)

        frozen_time.tick(delta=timedelta(seconds=60))

        assert datetime.now(UTC) == datetime(2025, 7, 2, 0, 1, 0, tzinfo=UTC)
