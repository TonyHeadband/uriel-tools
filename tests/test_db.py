import asyncio

import pytest

from uriel_tools.db import apply_migrations, make_pool

pytestmark = pytest.mark.db


async def test_migrations_are_idempotent(pool):
    assert await apply_migrations(pool) == []


async def test_concurrent_first_migrations_do_not_race(pg_dsn):
    """Three processes (events, indexer, mcp) can all start against a fresh database at once."""
    for _ in range(3):
        pools = [make_pool(pg_dsn) for _ in range(3)]
        for p in pools:
            await p.open()
        try:
            await asyncio.gather(*(apply_migrations(p) for p in pools))
        finally:
            for p in pools:
                await p.close()
    check = make_pool(pg_dsn)
    await check.open()
    async with check.connection() as conn:
        rows = await (
            await conn.execute("SELECT version, count(*) AS n FROM schema_migrations GROUP BY version")
        ).fetchall()
    await check.close()
    assert rows and all(r["n"] == 1 for r in rows)


async def test_schema_has_the_rag_tables(pool):
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        ).fetchall()
    assert {"folders", "documents", "chunks", "jobs", "schema_migrations"} <= {r["table_name"] for r in rows}


async def test_tsv_is_generated_from_text(pool):
    async with pool.connection() as conn:
        f = await (
            await conn.execute("INSERT INTO folders (mount, owners) VALUES ('/D', '{a}') RETURNING id")
        ).fetchone()
        d = await (
            await conn.execute(
                "INSERT INTO documents (folder_id, path, status)"
                " VALUES (%s, '/D/x.txt', 'indexed') RETURNING id",
                (f["id"],),
            )
        ).fetchone()
        await conn.execute(
            "INSERT INTO chunks (document_id, ord, page, source, text, embedding, embed_model) "
            "VALUES (%s, 0, 1, 'text', 'Polizza numero AB-1234', %s::vector, 'm')",
            (d["id"], "[" + ",".join(["0"] * 1023 + ["1"]) + "]"),
        )
        hit = await (
            await conn.execute(
                "SELECT count(*) AS n FROM chunks WHERE tsv @@ websearch_to_tsquery('simple', 'polizza')"
            )
        ).fetchone()
    assert hit["n"] == 1
