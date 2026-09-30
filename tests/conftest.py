import os
import uuid

import psycopg
import pytest

from uriel_tools.db import apply_migrations, make_pool

pytest_plugins = ["tests.schedule_helpers"]  # the `clock` fixture, shared by the schedule tests


@pytest.fixture
async def pg_dsn():
    """A throwaway database per test; skipped when no Postgres is configured."""
    admin = os.environ.get("URIEL_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("URIEL_TEST_DATABASE_URL not set")
    name = f"uriel_tools_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
        await conn.execute(f'CREATE DATABASE "{name}"')
    base, _, _ = admin.rpartition("/")
    yield f"{base}/{name}"
    async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as conn:
        await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
async def pool(pg_dsn):
    p = make_pool(pg_dsn)
    await p.open()
    await apply_migrations(p)
    yield p
    await p.close()
