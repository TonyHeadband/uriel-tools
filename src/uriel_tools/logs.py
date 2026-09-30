import logging


def configure_logging() -> None:
    # force=True: reconfigures even if a handler is already attached to the root logger (e.g. under a
    # test runner), so this is idempotent and testable, not just correct on a fresh process.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)
    # httpx/httpcore log every request at INFO, including the WebDAV URL (which carries the document path).
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
