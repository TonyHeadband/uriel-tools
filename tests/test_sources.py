from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from uriel_tools.config import RagSettings, load_yaml
from uriel_tools.rag.models import ModelsConfig
from uriel_tools.rag.sources import OcrWindow, SourcesConfig, next_daily, within


def test_share_settings_defaults():
    cfg = SourcesConfig.model_validate({})
    assert (cfg.share_poll_minutes, cfg.share_group_prefix) == (5, "uriel-")


def test_a_leftover_folder_list_fails_loudly():
    with pytest.raises(ValidationError, match="sharing them with uriel"):
        SourcesConfig.model_validate({"folders": [{"owner": "a", "path": "/Documents"}]})


def test_within_is_segment_aware():
    assert within("/Documents", "/Documents/a.pdf")
    assert within("/Documents", "/Documents")
    assert not within("/Documents", "/Documents2/a.pdf")
    assert within("/", "/anything")


def utc(h, m=0, day=10):
    return datetime(2026, 1, day, h, m, tzinfo=UTC)


def test_window_same_day():
    w = OcrWindow(start=time(1), end=time(6), tz="UTC")
    assert w.contains(utc(3)) and not w.contains(utc(6)) and not w.contains(utc(0, 59))
    assert w.next_start(utc(3)) == utc(3)
    assert w.next_start(utc(12)) == utc(1, day=11)
    assert w.next_start(utc(0, 30)) == utc(1)


def test_window_across_midnight():
    w = OcrWindow(start=time(22), end=time(4), tz="UTC")
    assert w.contains(utc(23)) and w.contains(utc(2)) and not w.contains(utc(12))
    assert w.next_start(utc(12)) == utc(22)


def test_window_uses_its_timezone():
    # 01:00 Europe/Rome in January is 00:00 UTC
    w = OcrWindow(start=time(1), end=time(6), tz="Europe/Rome")
    assert w.contains(utc(0, 30))
    assert not w.contains(utc(5, 30))


def test_next_daily_is_strictly_after_now():
    assert next_daily(utc(0, 30), time(0, 30), "UTC") == utc(0, 30, day=11)
    assert next_daily(utc(0, 10), time(0, 30), "UTC") == utc(0, 30)


def test_repo_config_files_load():
    assert load_yaml(SourcesConfig, "config/sources.yaml").share_group_prefix == "uriel-"
    m = load_yaml(ModelsConfig, "tests/e2e/models.e2e.yaml")
    assert m.has("embedding") and m.has("ocr") and not m.has("rerank")


def test_shared_models_file_with_other_processes_roles():
    # The deployment-wide file also carries the gateway's chat roles, deciders and provider fields.
    shared = {
        "models": {
            "chat": {"provider": "openai_compat", "base_url": "http://x/v1", "model": "qwen3:8b"},
            "emb": {"provider": "openai_compat", "base_url": "http://x/v1", "model": "qwen3-embedding:0.6b"},
            "vl": {"provider": "openai_compat", "base_url": "http://x/v1", "model": "qwen3-vl:8b"},
        },
        "roles": {"interactive": "chat", "background": "chat", "embedding": "emb", "ocr": "vl"},
        "deciders": {"route": {"adapter": "llm", "model": "chat"}},
    }
    m = ModelsConfig.model_validate(shared)
    assert m.role("embedding").model == "qwen3-embedding:0.6b"


def test_models_config_requires_embedding_and_ocr():
    spec = {"base_url": "http://x/v1", "model": "m"}
    with pytest.raises(ValidationError, match="ocr"):
        ModelsConfig.model_validate({"models": {"a": spec}, "roles": {"embedding": "a"}})
    with pytest.raises(ValidationError, match="unknown"):
        ModelsConfig.model_validate({"models": {"a": spec}, "roles": {"embedding": "a", "ocr": "b"}})


def test_rag_settings_env(monkeypatch):
    monkeypatch.setenv("URIEL_TOOLS_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("URIEL_NC_URL", "https://cloud.example")
    monkeypatch.setenv("URIEL_EVENTS_PORT", "tcp://10.96.0.20:8002")  # kubelet service link must not collide
    s = RagSettings()
    assert s.tools_database_url == "postgresql://x/y"
    assert s.events_listen_port == 8002
    assert (s.nc_user, s.nc_app_password) == ("uriel", "")
