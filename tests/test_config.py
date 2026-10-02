import pytest
from pydantic import ValidationError

from uriel_tools.app import build_mcp
from uriel_tools.config import McpSettings, ScheduleSettings, load_yaml
from uriel_tools.gating import ToolPolicy
from uriel_tools.memory.questionnaire import Questionnaire


def test_mcp_settings_survive_kubelet_service_link_env_vars(monkeypatch):
    # A Service named `uriel-mcp` makes the kubelet inject URIEL_MCP_PORT=tcp://10.96.0.10:8001,
    # which would collide with a field named `port` and crash-loop the pod.
    monkeypatch.setenv("URIEL_MCP_PORT", "tcp://10.96.0.10:8001")
    monkeypatch.setenv("URIEL_MCP_API_KEYS", "x")
    s = McpSettings()
    assert s.listen_port == 8001


def test_the_shipped_questionnaire_loads():
    q = load_yaml(Questionnaire, McpSettings(api_keys="k").questionnaire_file)
    assert [x.section for x in q.user] == ["Name", "Birthday", "Music", "Hobbies", "Food"]
    assert [x.section for x in q.soul] == ["Tone", "Length", "Language", "Humour", "Avoid"]
    assert [x.section for x in q.user + q.soul if x.replace] == [
        "Name",
        "Birthday",
        "Tone",
        "Length",
        "Language",
        "Humour",
    ]


def test_a_tool_without_a_category_is_refused():
    with pytest.raises(ValidationError, match="category"):
        ToolPolicy.model_validate({"tools": {"x": {"groups": ["family"]}}})


def test_every_shipped_tool_has_a_category():
    policy = load_yaml(ToolPolicy, McpSettings(api_keys="k").tools_file)
    assert {r.category for r in policy.tools.values()} == {
        "calendar",
        "homelab",
        "camera",
        "documents",
        "memory",
        "reporting",
        "schedules",
        "web",
    }


def test_an_unknown_companion_action_is_refused():
    with pytest.raises(ValidationError, match="companion_action"):
        ToolPolicy.model_validate(
            {"tools": {"x": {"groups": ["family"], "category": "web", "companion_action": "dance"}}}
        )


def test_every_shipped_tool_the_model_sees_has_a_companion_action():
    policy = load_yaml(ToolPolicy, McpSettings(api_keys="k").tools_file)
    assert {n for n, r in policy.tools.items() if r.companion_action is None} == {
        n for n, r in policy.tools.items() if r.hidden
    }
    by_action = {}
    for n, r in policy.tools.items():
        by_action.setdefault(r.companion_action, set()).add(n)
    assert by_action["draft"] == {
        "draft_issue",
        "draft_schedule",
        "draft_calendar_change",
        "preview_signature",
    }
    assert by_action["confirm"] == {
        "file_issue",
        "create_schedule",
        "confirm_calendar_change",
        "place_signature",
        "confirm_edit",
    }
    assert by_action["ask"] == {"next_question"}


async def test_the_shipped_tool_policy_covers_every_tool():
    policy = load_yaml(ToolPolicy, McpSettings(api_keys="k").tools_file)
    registered = {t.name for t in await build_mcp(policy).list_tools(run_middleware=False)}
    assert set(policy.tools) == registered
    assert policy.unattended_tools() == {
        "web_search",
        "search_documents",
        "show_memory",
        "list_schedules",
        "calendar_agenda",
    }
    assert {n for n, r in policy.tools.items() if r.hidden} == {
        "memory_context",
        "claim_due_runs",
        "finish_run",
    }
    assert policy.tools["claim_due_runs"].groups == ["uriel-internal"]
    assert policy.tools["finish_run"].groups == ["uriel-internal"]


def test_the_mirror_is_on_with_nextcloud_unless_turned_off(monkeypatch):
    assert ScheduleSettings().mirror_on(True) is True
    assert ScheduleSettings().mirror_on(False) is False
    monkeypatch.setenv("URIEL_MIRROR_ENABLED", "false")
    assert ScheduleSettings().mirror_on(True) is False
    monkeypatch.setenv("URIEL_MIRROR_ENABLED", "true")
    assert ScheduleSettings().mirror_on(False) is False
