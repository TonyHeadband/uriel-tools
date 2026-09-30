import logging

import pytest

from uriel_tools.logs import configure_logging


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    httpx_level, httpcore_level = logging.getLogger("httpx").level, logging.getLogger("httpcore").level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpcore_level)


def test_configure_logging_silences_httpx_but_not_uriel_tools(restore_logging):
    configure_logging()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    assert logging.getLogger("uriel_tools").isEnabledFor(logging.INFO)
