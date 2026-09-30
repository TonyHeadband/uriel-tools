import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastmcp import Client

from tests.fakes.models import HashEmbedder
from tests.schedule_helpers import NEWS, NOW, make, unattended
from uriel_tools.app import build_mcp
from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller, ToolPolicy
from uriel_tools.issues import Forgejo
from uriel_tools.schedules.service import Refused, Schedules
from uriel_tools.tools import Rag

pytestmark = pytest.mark.db

TONY = Caller("anthony-headband", frozenset({"family"}), "r1", sub="s-tony")
LIL_B = Caller("lil-b", frozenset({"family"}), "r2")
NOBODY = Caller(None, frozenset({"family"}), "r3")
UNATTENDED = unattended("web_search", "search_documents", "show_memory", "list_schedules")


@pytest.fixture
def svc(pool, clock):
    return Schedules(pool, Drafts(), UNATTENDED, "America/Toronto", clock)


async def test_a_draft_shows_what_will_run_and_schedules_nothing(svc, pool):
    draft = await svc.draft(TONY, **NEWS)
    assert draft["when"] == "every day at 9:00 (Toronto time)"
    assert draft["tools"] == ["web_search"] and draft["tz"] == "America/Toronto"
    assert [r["at"] for r in draft["next_runs"]] == [
        "2026-03-07T14:00:00Z",
        "2026-03-08T13:00:00Z",
        "2026-03-09T13:00:00Z",
    ]
    assert draft["next_runs"][0]["at_local"] == "Sat 7 Mar 2026, 9:00"
    assert "only after they say yes" in draft["next"]
    assert (await svc.list(TONY))["schedules"] == []


async def test_create_makes_exactly_the_draft_once(svc):
    draft = await svc.draft(TONY, **NEWS)
    first = await svc.create(TONY, draft["draft_id"])
    again = await svc.create(TONY, draft["draft_id"])
    assert again == first
    assert first["created"] is True
    assert (first["title"], first["when"], first["next_run_at"]) == (
        "AI news",
        "every day at 9:00 (Toronto time)",
        "2026-03-07T14:00:00Z",
    )
    [listed] = (await svc.list(TONY))["schedules"]
    assert listed["schedule_id"] == first["schedule_id"] and listed["last_run"] is None


async def test_a_cancelled_schedule_is_not_created_again_from_its_draft(svc, pool, clock):
    draft = await svc.draft(TONY, **NEWS)
    made = await svc.create(TONY, draft["draft_id"])
    assert made["status"] == "active"
    await svc.cancel(TONY, made["schedule_id"])
    with pytest.raises(Refused, match="was cancelled: call draft_schedule again"):
        await svc.create(TONY, draft["draft_id"])
    other = Schedules(pool, Drafts(), UNATTENDED, "America/Toronto", clock)  # a server without the cache
    other_id = other.drafts.add(TONY, svc.drafts.get(TONY, draft["draft_id"]).draft)
    with pytest.raises(Refused, match="was cancelled"):
        await other.create(TONY, other_id)
    assert (await svc.list(TONY))["schedules"] == []


async def test_a_new_draft_store_still_creates_one_schedule(svc, pool, clock):
    """Two servers (or a restart between two create calls) share nothing but the caldav_uid."""
    draft = await svc.draft(TONY, **NEWS)
    stored = svc.drafts.get(TONY, draft["draft_id"])
    other = Schedules(pool, Drafts(), UNATTENDED, "America/Toronto", clock)
    other_id = other.drafts.add(TONY, stored.draft)
    a = await svc.create(TONY, draft["draft_id"])
    b = await other.create(TONY, other_id)
    assert a["schedule_id"] == b["schedule_id"]


async def test_drafts_and_schedules_belong_to_their_owner(svc):
    draft = await svc.draft(TONY, **NEWS)
    with pytest.raises(Refused, match="no such draft"):
        await svc.create(LIL_B, draft["draft_id"])
    mine = await svc.create(TONY, draft["draft_id"])
    assert (await svc.list(LIL_B))["schedules"] == []
    with pytest.raises(Refused, match="no schedule"):
        await svc.cancel(LIL_B, mine["schedule_id"])
    with pytest.raises(Refused, match="no schedule"):
        await svc.runs(LIL_B, schedule_id=mine["schedule_id"])
    assert len((await svc.list(TONY))["schedules"]) == 1


async def test_schedules_need_a_signed_in_person(svc):
    with pytest.raises(Refused, match="signed-in person"):
        await svc.draft(NOBODY, **NEWS)
    with pytest.raises(Refused, match="signed-in person"):
        await svc.list(NOBODY)


async def test_only_unattended_tools_can_be_scheduled(svc):
    with pytest.raises(Refused, match="can't run unattended: file_issue, made_up") as e:
        await svc.draft(TONY, **NEWS, tools=["web_search", "file_issue", "made_up"])
    assert "web_search" in str(e.value)  # says which ones can
    draft = await svc.draft(TONY, **NEWS, tools=["search_documents", "search_documents"])
    assert draft["tools"] == ["search_documents"]
    assert (await svc.draft(TONY, **NEWS, tools=[]))["tools"] == []


async def test_a_schedule_can_only_use_tools_the_person_may_use(pool, clock):
    kid = Caller("kid", frozenset({"kids"}), "r4")
    policy = ToolPolicy.model_validate(
        {
            "tools": {
                "web_search": {"groups": ["family", "kids"], "category": "test", "unattended": True},
                "search_documents": {"groups": ["family"], "category": "test", "unattended": True},
            }
        }
    )
    svc = Schedules(pool, Drafts(), policy, "America/Toronto", clock)
    with pytest.raises(Refused, match="not available to you: search_documents") as e:
        await svc.draft(kid, **NEWS, tools=["web_search", "search_documents"])
    assert "web_search" in str(e.value)  # says which ones are
    assert (await svc.draft(kid, **NEWS))["tools"] == ["web_search"]
    assert (await svc.draft(TONY, **NEWS, tools=["search_documents"]))["tools"] == ["search_documents"]


async def test_bad_fields_are_refused_with_a_fix(svc):
    with pytest.raises(Refused, match="HH:MM"):
        await svc.draft(TONY, **(NEWS | {"at": "9am"}))
    with pytest.raises(Refused, match="already passed"):
        await svc.draft(TONY, **(NEWS | {"frequency": "once"}), on="2026-03-06")
    with pytest.raises(Refused, match="title and a prompt"):
        await svc.draft(TONY, **(NEWS | {"title": "   "}))
    with pytest.raises(Refused, match="120"):
        await svc.draft(TONY, **(NEWS | {"title": "x" * 121}))


async def test_a_one_off_whose_time_passes_before_the_yes_is_not_created(svc, clock):
    draft = await svc.draft(TONY, **(NEWS | {"frequency": "once", "at": "10:30"}), on="2026-03-06")
    clock.at = NOW + timedelta(hours=1)  # 11:00 in Toronto
    with pytest.raises(Refused, match="already passed"):
        await svc.create(TONY, draft["draft_id"])
    assert (await svc.list(TONY))["schedules"] == []


async def test_cancel_stops_a_schedule(svc):
    made = await make(svc, TONY)
    out = await svc.cancel(TONY, made["schedule_id"])
    assert out == {"schedule_id": made["schedule_id"], "title": "AI news", "status": "cancelled"}
    assert (await svc.list(TONY))["schedules"] == []
    assert (await svc.cancel(TONY, made["schedule_id"]))["status"] == "cancelled"  # again: no error


async def test_set_timezone_is_used_by_new_drafts_and_leaves_old_schedules(svc):
    old = await make(svc, TONY)
    out = await svc.set_timezone(TONY, "europe/rome")
    assert out["tz"] == "Europe/Rome"
    assert out["unchanged_schedules"] == [
        {"schedule_id": old["schedule_id"], "title": "AI news", "tz": "America/Toronto"}
    ]
    assert "redraft" in out["note"]
    draft = await svc.draft(TONY, **NEWS)
    assert draft["tz"] == "Europe/Rome" and draft["when"] == "every day at 9:00 (Rome time)"
    with pytest.raises(Refused, match="not a timezone"):
        await svc.set_timezone(TONY, "Mars/Olympus")


# Through the MCP server.

POLICY = ToolPolicy.model_validate(
    {
        "tools": {
            t: {"groups": ["family"], "category": "test"}
            for t in [
                "draft_schedule",
                "create_schedule",
                "list_schedules",
                "list_runs",
                "cancel_schedule",
                "set_timezone",
                "draft_issue",
            ]
        }
        | {"web_search": {"groups": ["family"], "category": "test", "unattended": True}}
    }
)
TONY_META = {"user": "anthony-headband", "groups": ["family"], "request_id": "r1"}
LIL_B_META = {"user": "lil-b", "groups": ["family"], "request_id": "r2"}
WEEKLY = {
    "title": "AI news",
    "prompt": "A digest of AI news",
    "frequency": "weekly",
    "time": "09:00",
    "weekdays": ["mon", "thu"],
}


class Chat:
    """One MCP session: drafts live in the server between calls."""

    def __init__(self, rag, issues=None, policy=POLICY):
        self.client = Client(build_mcp(policy, rag, issues))

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def call(self, tool, args, meta=TONY_META):
        return await self.client.call_tool(tool, args, meta=meta, raise_on_error=False)


async def test_draft_then_create_through_the_server(pool):
    async with Chat(Rag(pool, HashEmbedder())) as chat:
        draft = (await chat.call("draft_schedule", WEEKLY)).structured_content
        assert draft["when"] == "every Monday and Thursday at 9:00 (Toronto time)"
        assert draft["tools"] == ["web_search"]
        made = await chat.call("create_schedule", {"draft_id": draft["draft_id"]})
        assert not made.is_error and made.structured_content["created"] is True
        stranger = await chat.call("list_schedules", {}, LIL_B_META)
        assert stranger.structured_content == {"schedules": []}
        [mine] = (await chat.call("list_schedules", {})).structured_content["schedules"]
        assert mine["title"] == "AI news"
        runs = await chat.call("list_runs", {"schedule_id": mine["schedule_id"]})
        assert runs.structured_content == {"runs": []}
        tz = await chat.call("set_timezone", {"tz": "Europe/Rome"})
        assert tz.structured_content["unchanged_schedules"][0]["schedule_id"] == mine["schedule_id"]
        gone = await chat.call("cancel_schedule", {"schedule_id": mine["schedule_id"]})
        assert gone.structured_content["status"] == "cancelled"


async def test_an_issue_draft_id_cannot_create_a_schedule(pool):
    tracker = Forgejo(
        "https://git.x", "t0k", "me/r", transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    async with Chat(Rag(pool, HashEmbedder()), tracker) as chat:
        issue = await chat.call("draft_issue", {"title": "x", "kind": "bug", "description": "y"})
        res = await chat.call("create_schedule", {"draft_id": issue.structured_content["draft_id"]})
        assert res.is_error and "draft_schedule again" in res.content[0].text
        assert (await chat.call("list_schedules", {})).structured_content == {"schedules": []}


async def test_schedule_errors_are_readable(pool):
    async with Chat(Rag(pool, HashEmbedder())) as chat:
        anonymous = await chat.call("list_schedules", {}, {"groups": ["family"]})
        assert anonymous.is_error and "signed-in person" in anonymous.content[0].text
        bad = await chat.call("draft_schedule", WEEKLY | {"tools": ["file_issue"]})
        assert bad.is_error and "can't run unattended: file_issue" in bad.content[0].text
        weekday = await chat.call("draft_schedule", WEEKLY | {"weekdays": ["funday"]})
        assert weekday.is_error and "funday" in weekday.content[0].text


async def test_schedules_without_a_database_say_not_configured():
    async with Chat(None) as chat:
        res = await chat.call("list_schedules", {})
    assert res.is_error and "scheduling is not configured" in res.content[0].text


# Claiming and finishing runs (the gateway's runner).

SAT_9 = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)  # the first run of a daily 9:00: Saturday 7 March, EST


async def statuses(pool) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT due_at, status FROM schedule_runs ORDER BY due_at")
        ).fetchall()
    return [(r["due_at"].astimezone(UTC).strftime("%m-%d %H:%M"), r["status"]) for r in rows]


async def test_only_due_runs_are_claimed(svc, clock):
    await make(svc, TONY)
    assert (await svc.claim_due())["runs"] == []
    clock.at = SAT_9 + timedelta(minutes=1)
    [run] = (await svc.claim_due())["runs"]
    assert run["owner"] == "anthony-headband" and run["owner_sub"] == "s-tony"
    assert (run["title"], run["prompt"], run["tools"], run["tz"]) == (
        "AI news",
        NEWS["prompt"],
        ["web_search"],
        "America/Toronto",
    )
    assert (run["due_at"], run["due_at_local"]) == ("2026-03-07T14:00:00Z", "Sat 7 Mar 2026, 9:00")
    assert (await svc.claim_due())["runs"] == []  # the same occurrence is never handed out twice
    [listed] = (await svc.list(TONY))["schedules"]
    assert listed["next_run_at"] == "2026-03-08T13:00:00Z"  # Sunday, now EDT
    assert listed["last_run"]["status"] == "claimed"


async def test_two_claimers_get_one_run_per_occurrence(svc, clock):
    for i in range(3):
        await make(svc, TONY, title=f"news {i}")
    clock.at = SAT_9 + timedelta(minutes=1)
    a, b = await asyncio.gather(svc.claim_due(), svc.claim_due())
    ids = [r["run_id"] for r in a["runs"] + b["runs"]]
    assert len(ids) == len(set(ids)) == 3


async def test_the_limit_leaves_the_rest_for_the_next_call(svc, clock):
    for i in range(3):
        await make(svc, TONY, title=f"news {i}")
    clock.at = SAT_9 + timedelta(minutes=1)
    assert len((await svc.claim_due(limit=2))["runs"]) == 2
    assert len((await svc.claim_due(limit=2))["runs"]) == 1


async def test_after_downtime_missed_runs_are_skipped_and_only_the_latest_runs(svc, pool, clock):
    await make(svc, TONY)
    clock.at = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)  # Tuesday 9:30 EDT: Sat, Sun and Mon were missed
    [run] = (await svc.claim_due())["runs"]
    assert run["due_at"] == "2026-03-10T13:00:00Z"
    assert await statuses(pool) == [
        ("03-07 14:00", "skipped"),
        ("03-08 13:00", "skipped"),
        ("03-09 13:00", "skipped"),
        ("03-10 13:00", "claimed"),
    ]
    async with pool.connection() as conn:
        dirty = await (
            await conn.execute("SELECT count(*) AS n FROM schedule_runs WHERE mirror_dirty")
        ).fetchone()
    assert dirty["n"] == 3  # the skipped ones show as done-tasks, so a missed run doesn't silently vanish


async def test_a_run_later_than_its_grace_is_skipped(svc, pool, clock):
    await make(svc, TONY)
    clock.at = SAT_9 + timedelta(hours=2, minutes=1)
    assert (await svc.claim_due())["runs"] == []
    assert await statuses(pool) == [("03-07 14:00", "skipped")]
    [listed] = (await svc.list(TONY))["schedules"]
    assert listed["next_run_at"] == "2026-03-08T13:00:00Z"
    assert listed["last_run"]["status"] == "skipped"


async def test_a_one_off_runs_once_then_is_finished(svc, clock):
    made = await make(svc, TONY, frequency="once", at="10:30", on="2026-03-06")
    clock.at = NOW + timedelta(minutes=35)
    [run] = (await svc.claim_due())["runs"]
    assert run["due_at"] == "2026-03-06T15:30:00Z"
    assert (await svc.list(TONY))["schedules"] == []
    assert (await svc.cancel(TONY, made["schedule_id"]))["status"] == "finished"


async def test_a_one_off_missed_by_more_than_ten_minutes_is_skipped(svc, pool, clock):
    await make(svc, TONY, frequency="once", at="10:30", on="2026-03-06")
    clock.at = NOW + timedelta(minutes=41)
    assert (await svc.claim_due())["runs"] == []
    assert await statuses(pool) == [("03-06 15:30", "skipped")]


async def test_an_expired_lease_is_never_handed_out_again_and_fails(svc, pool, clock):
    await make(svc, TONY)
    clock.at = SAT_9 + timedelta(minutes=1)
    [run] = (await svc.claim_due())["runs"]
    clock.at += timedelta(minutes=11)
    assert (await svc.claim_due())["runs"] == []
    [listed] = (await svc.runs(TONY))["runs"]
    assert (listed["run_id"], listed["status"]) == (run["run_id"], "failed")
    assert listed["error"] == "lease expired (gateway lost the run)"
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT mirror_dirty FROM schedule_runs WHERE id = %s", (run["run_id"],))
        ).fetchone()
    assert row["mirror_dirty"] is True  # mirrored like any other failed run
    late = await svc.finish(run["run_id"], "ok", summary="too late")
    assert late == {"run_id": run["run_id"], "status": "failed", "finished": False}


async def test_finish_records_the_outcome_once(svc, clock):
    await make(svc, TONY)
    clock.at = SAT_9 + timedelta(minutes=1)
    [run] = (await svc.claim_due())["runs"]
    clock.at += timedelta(minutes=2)
    done = await svc.finish(run["run_id"], "ok", summary="Three stories", talk_message_id=77)
    assert done == {"run_id": run["run_id"], "status": "ok", "finished": True}
    retried = await svc.finish(run["run_id"], "failed", error="late retry")
    assert retried == {"run_id": run["run_id"], "status": "ok", "finished": False}
    [listed] = (await svc.runs(TONY))["runs"]
    assert (listed["status"], listed["summary"], listed["error"]) == ("ok", "Three stories", None)
    assert listed["finished_at"] == "2026-03-07T14:03:00Z"
    with pytest.raises(Refused, match="no run"):
        await svc.finish(999999, "ok")
    with pytest.raises(Refused, match="status must be"):
        await svc.finish(run["run_id"], "claimed")


async def test_list_runs_filters_by_schedule_and_since(svc, clock):
    news = await make(svc, TONY)
    await make(svc, TONY, title="Weather", at="07:00")
    clock.at = datetime(2026, 3, 7, 14, 1, tzinfo=UTC)
    await (
        svc.claim_due()
    )  # Saturday: weather's 7:00 is 2 h 1 min late, past its 2 h grace (skipped), news runs
    clock.at = datetime(2026, 3, 8, 13, 1, tzinfo=UTC)
    await svc.claim_due()  # Sunday: news runs, weather skipped again
    everything = (await svc.runs(TONY))["runs"]
    assert [r["due_at"] for r in everything] == sorted((r["due_at"] for r in everything), reverse=True)
    assert len(everything) == 4
    news_runs = (await svc.runs(TONY, schedule_id=news["schedule_id"]))["runs"]
    assert [r["title"] for r in news_runs] == ["AI news", "AI news"]
    recent = (await svc.runs(TONY, since="2026-03-08T00:00:00Z"))["runs"]
    assert {r["due_at"][:10] for r in recent} == {"2026-03-08"}
    with pytest.raises(Refused, match="ISO 8601"):
        await svc.runs(TONY, since="yesterday")


async def test_the_runner_tools_through_the_server(pool):
    policy = ToolPolicy.model_validate(
        {
            "tools": {
                "claim_due_runs": {"groups": ["uriel-internal"], "category": "test"},
                "finish_run": {"groups": ["uriel-internal"], "category": "test"},
            }
        }
    )
    gateway = {"user": "uriel-gateway", "groups": ["uriel-internal"], "request_id": "r9"}
    async with Chat(Rag(pool, HashEmbedder()), policy=policy) as chat:
        claimed = await chat.call("claim_due_runs", {"limit": 5}, gateway)
        assert claimed.structured_content == {"runs": []}
        missing = await chat.call("finish_run", {"run_id": 1, "status": "ok"}, gateway)
        assert missing.is_error and "no run 1" in missing.content[0].text
        bad = await chat.call("finish_run", {"run_id": 1, "status": "claimed"}, gateway)
        assert bad.is_error
        person = await chat.call("claim_due_runs", {}, TONY_META)
        assert person.is_error and "not permitted" in person.content[0].text
