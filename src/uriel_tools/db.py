from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"
_MIGRATION_LOCK = 7_272_742  # arbitrary advisory-lock id; serialises the three processes' startups


def make_pool(dsn: str, max_size: int = 5, timeout: float = 10.0) -> AsyncConnectionPool:
    # A short timeout so a tool call fails fast with a readable error when Postgres is down.
    return AsyncConnectionPool(
        dsn,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )


async def apply_migrations(pool: AsyncConnectionPool, directory: Path = MIGRATIONS) -> list[str]:
    applied: list[str] = []
    async with pool.connection() as conn:
        # Lock before creating the table: concurrent first starts (events + indexer + mcp) racing on
        # CREATE TABLE IF NOT EXISTS otherwise raise UniqueViolation on pg_type.
        await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            rows = await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()
            done = {r["version"] for r in rows}
            # Materialize glob results before async operations
            migration_files = sorted(list(directory.glob("*.sql")))  # noqa: ASYNC240
            for path in migration_files:
                if path.stem in done:
                    continue
                async with conn.transaction():
                    # prepare=False uses the simple query protocol, allowing multiple statements
                    await conn.execute(path.read_text(), prepare=False)
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied.append(path.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied
