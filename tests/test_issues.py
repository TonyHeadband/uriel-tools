import json

import httpx
from fastmcp import Client
from mcp.types import PaginatedRequestParams

from uriel_tools.app import build_mcp
from uriel_tools.gating import ToolPolicy
from uriel_tools.issues import Forgejo

POLICY = ToolPolicy.model_validate(
    {
        "tools": {
            "draft_issue": {"groups": ["family"], "category": "test"},
            "file_issue": {"groups": ["family"], "category": "test"},
        }
    }
)
MUM = {"user": "mum", "groups": ["family"], "request_id": "req-42"}
DAD = {"user": "dad", "groups": ["family"], "request_id": "req-43"}
GUEST = {"user": "guest", "groups": [], "request_id": "r3"}
BUG = {
    "title": "Signature went in the wrong box",
    "kind": "bug",
    "description": "I asked Uriel to sign the school form and it signed in the father's box.",
    "what_happened": "sign_document placed the signature on Signature_2",
}


class FakeForgejo:
    """The two Forgejo endpoints file_issue uses, recording what was posted."""

    def __init__(self, labels=("kind/bug", "kind/feature", "from-chat"), fail: int | None = None):
        self.labels = {name: i + 1 for i, name in enumerate(labels)}
        self.fail = fail
        self.posted: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "token t0k"
        if self.fail:
            return httpx.Response(self.fail, json={"message": "boom"})
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/repos/me/uriel-agent/labels":
            return httpx.Response(200, json=[{"id": i, "name": n} for n, i in self.labels.items()])
        if request.method == "POST" and path == "/api/v1/repos/me/uriel-agent/issues":
            self.posted.append(json.loads(request.content))
            n = len(self.posted)
            return httpx.Response(
                201, json={"number": n, "html_url": f"https://git.x/me/uriel-agent/issues/{n}"}
            )
        return httpx.Response(404)

    def client(self) -> Forgejo:
        return Forgejo("https://git.x", "t0k", "me/uriel-agent", transport=httpx.MockTransport(self.handler))


class Chat:
    """One MCP session, like one conversation: drafts live in the server between calls."""

    def __init__(self, issues):
        self.client = Client(build_mcp(POLICY, issues=issues))

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def call(self, tool, args, meta=MUM):
        return await self.client.call_tool(tool, args, meta=meta, raise_on_error=False)


async def test_unconfigured_is_a_readable_error():
    async with Chat(None) as chat:
        res = await chat.call("draft_issue", BUG)
    assert res.is_error
    assert "not configured" in res.content[0].text


async def test_draft_returns_the_exact_issue_and_sends_nothing():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        res = await chat.call("draft_issue", BUG)
    assert not res.is_error
    out = res.structured_content
    assert out["draft_id"]
    assert out["title"] == BUG["title"]
    assert out["labels"] == ["kind/bug", "from-chat"]
    for text in (BUG["description"], BUG["what_happened"], "mum", "req-42"):
        assert text in out["body"]
    assert "Nothing has been sent" in out["next"]
    assert fj.posted == []


async def test_filing_posts_the_stored_draft():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        draft = (await chat.call("draft_issue", BUG)).structured_content
        res = await chat.call("file_issue", {"draft_id": draft["draft_id"]})
    assert not res.is_error
    assert res.structured_content == {
        "filed": True,
        "number": 1,
        "url": "https://git.x/me/uriel-agent/issues/1",
    }
    [issue] = fj.posted
    assert (issue["title"], issue["body"], issue["labels"]) == (draft["title"], draft["body"], [1, 3])


async def test_filing_twice_returns_the_same_issue():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        draft_id = (await chat.call("draft_issue", BUG)).structured_content["draft_id"]
        first = await chat.call("file_issue", {"draft_id": draft_id})
        again = await chat.call("file_issue", {"draft_id": draft_id})
    assert again.structured_content == first.structured_content
    assert len(fj.posted) == 1


async def test_an_unknown_draft_cannot_be_filed():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        res = await chat.call("file_issue", {"draft_id": "made-up"})
    assert res.is_error
    assert "draft_issue" in res.content[0].text  # tells the model what to do instead
    assert fj.posted == []


async def test_only_the_person_who_drafted_can_file():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        draft_id = (await chat.call("draft_issue", BUG)).structured_content["draft_id"]
        res = await chat.call("file_issue", {"draft_id": draft_id}, meta=DAD)
    assert res.is_error
    assert fj.posted == []


async def test_feature_request_gets_the_feature_label():
    fj = FakeForgejo()
    feature = {"title": "Remind me of bills", "kind": "feature", "description": "A reminder a week before."}
    async with Chat(fj.client()) as chat:
        draft = (await chat.call("draft_issue", feature)).structured_content
        await chat.call("file_issue", {"draft_id": draft["draft_id"]})
    assert fj.posted[0]["labels"] == [2, 3]
    assert "What happened" not in fj.posted[0]["body"]


async def test_missing_label_is_skipped():
    fj = FakeForgejo(labels=("kind/bug",))
    async with Chat(fj.client()) as chat:
        draft_id = (await chat.call("draft_issue", BUG)).structured_content["draft_id"]
        res = await chat.call("file_issue", {"draft_id": draft_id})
    assert not res.is_error
    assert fj.posted[0]["labels"] == [1]


async def test_forgejo_failure_is_a_friendly_error_and_the_draft_survives():
    fj = FakeForgejo(fail=500)
    async with Chat(fj.client()) as chat:
        draft_id = (await chat.call("draft_issue", BUG)).structured_content["draft_id"]
        res = await chat.call("file_issue", {"draft_id": draft_id})
        assert res.is_error
        assert "couldn't reach the issue tracker" in res.content[0].text
        fj.fail = None  # the tracker is back: the same draft can still be filed
        again = await chat.call("file_issue", {"draft_id": draft_id})
    assert again.structured_content["filed"] is True


async def test_long_title_is_capped():
    fj = FakeForgejo()
    async with Chat(fj.client()) as chat:
        draft = (await chat.call("draft_issue", BUG | {"title": "x" * 500})).structured_content
    assert len(draft["title"]) <= 120


async def test_only_allowed_groups_see_the_tools():
    async with Client(build_mcp(POLICY, issues=FakeForgejo().client())) as c:
        res = await c.session.list_tools(params=PaginatedRequestParams(_meta=GUEST))
        assert res.tools == []
