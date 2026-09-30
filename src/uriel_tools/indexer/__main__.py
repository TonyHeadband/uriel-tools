import asyncio
import signal

from uriel_tools.config import RagSettings, load_yaml
from uriel_tools.db import apply_migrations, make_pool
from uriel_tools.indexer.pipeline import Indexer
from uriel_tools.indexer.worker import Worker
from uriel_tools.logs import configure_logging
from uriel_tools.rag.embed import Embedder
from uriel_tools.rag.models import ModelsConfig
from uriel_tools.rag.ocr import OcrModel
from uriel_tools.rag.shares import Shares
from uriel_tools.rag.sources import SourcesConfig
from uriel_tools.rag.store import Folder
from uriel_tools.rag.webdav import WebDav


async def run() -> None:
    settings = RagSettings()
    if not settings.tools_database_url or not settings.nc_url or not settings.nc_app_password:
        raise SystemExit("URIEL_TOOLS_DATABASE_URL, URIEL_NC_URL and URIEL_NC_APP_PASSWORD are required")
    sources = load_yaml(SourcesConfig, settings.sources_file)
    models = load_yaml(ModelsConfig, settings.models_file)
    pool = make_pool(settings.tools_database_url)
    await pool.open()
    await apply_migrations(pool)
    embedder = Embedder(models.role("embedding"))

    def webdav_for(folder: Folder) -> WebDav:
        return WebDav(settings.nc_url, settings.nc_user, settings.nc_app_password)

    async def list_shares() -> list[dict]:
        client = Shares(settings.nc_url, settings.nc_user, settings.nc_app_password)
        try:
            return await client.shared_with_me()
        finally:
            await client.aclose()

    indexer = Indexer(pool, sources, webdav_for, embedder, OcrModel(models.role("ocr")))
    worker = Worker(pool, sources, indexer, embedder, list_shares)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run_forever(stop)
    finally:
        await pool.close()


def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
