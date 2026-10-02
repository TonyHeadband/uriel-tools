from datetime import UTC, datetime, timedelta

import pytest

from uriel_tools.calendars import store
from uriel_tools.schedules import store as schedule_store

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)


async def test_the_default_calendar_is_per_person(pool):
    assert await store.get_default(pool, "anthony-headband") is None
    await store.set_default(pool, "anthony-headband", "/c/uriel/p/")
    await store.set_default(pool, "anthony-headband", "/c/uriel/w/")
    assert await store.get_default(pool, "anthony-headband") == "/c/uriel/w/"
    assert await store.get_default(pool, "lil-b") is None


async def test_the_default_keeps_the_timezone(pool):
    await schedule_store.set_tz(pool, "anthony-headband", "Europe/Rome")
    await store.set_default(pool, "anthony-headband", "/c/uriel/p/")
    assert await schedule_store.get_tz(pool, "anthony-headband", "America/Toronto") == "Europe/Rome"


async def test_created_items_are_remembered_per_person(pool):
    await store.record_created(pool, "anthony-headband", "/c/uriel/p/uriel-1.ics", NOW)
    assert await store.created_since(
        pool, "anthony-headband", "/c/uriel/p/uriel-1.ics", NOW - timedelta(minutes=15)
    )
    assert not await store.created_since(
        pool, "anthony-headband", "/c/uriel/p/uriel-1.ics", NOW + timedelta(seconds=1)
    )
    assert not await store.created_since(pool, "lil-b", "/c/uriel/p/uriel-1.ics", NOW - timedelta(minutes=15))


async def test_old_records_are_dropped_on_write(pool):
    await store.record_created(pool, "o", "/old.ics", NOW - timedelta(days=2))
    await store.record_created(pool, "o", "/new.ics", NOW)
    async with pool.connection() as conn:
        rows = await (await conn.execute("SELECT item_path FROM calendar_created")).fetchall()
    assert [r["item_path"] for r in rows] == ["/new.ics"]
