import uvicorn

from uriel_tools.config import RagSettings
from uriel_tools.db import make_pool
from uriel_tools.events.app import build_events_app, source_keys
from uriel_tools.logs import configure_logging


def main() -> None:
    configure_logging()
    settings = RagSettings()
    if not settings.tools_database_url:
        raise SystemExit("URIEL_TOOLS_DATABASE_URL is required")
    keys = source_keys()
    if not keys:
        raise SystemExit("no URIEL_EVENTS_KEY_<SOURCE> is set")
    app = build_events_app(make_pool(settings.tools_database_url), keys, settle_s=settings.events_settle_s)
    uvicorn.run(app, host="0.0.0.0", port=settings.events_listen_port)


if __name__ == "__main__":
    main()
