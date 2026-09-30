import httpx
import pytest
from fastmcp import Client

from uriel_tools.app import build_mcp
from uriel_tools.gating import ToolPolicy
from uriel_tools.search import SearchUnavailable, WebSearch

POLICY = ToolPolicy.model_validate(
    {"tools": {"web_search": {"groups": ["family"], "category": "web", "unattended": True}}}
)
KID = {"user": "kid", "groups": ["family"], "request_id": "r1"}


def hit(n: int, url: str | None = None, **extra) -> dict:
    return {
        "title": f"Story {n}",
        "url": url or f"https://news.example/{n}",
        "content": f"About story {n}",
        "engine": "duckduckgo",
    } | extra


class FakeSearxng:
    """SearXNG's /search JSON API, recording the query strings it was sent."""

    def __init__(self, results: list[dict] | None = None, status: int = 200, timeout: bool = False):
        self.results, self.status, self.timeout = results or [], status, timeout
        self.params: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.timeout:
            raise httpx.ReadTimeout("slow", request=request)
        assert request.url.path == "/search"
        self.params.append(dict(request.url.params))
        return httpx.Response(self.status, json={"query": "x", "results": self.results})

    def client(self) -> WebSearch:
        return WebSearch("https://searx.example/", transport=httpx.MockTransport(self.handler))


async def test_results_are_mapped():
    fake = FakeSearxng([hit(1, publishedDate="2026-09-29T08:00:00"), hit(2)])
    out = await fake.client().search("chips")
    assert out == {
        "query": "chips",
        "results": [
            {
                "title": "Story 1",
                "url": "https://news.example/1",
                "snippet": "About story 1",
                "engine": "duckduckgo",
                "published": "2026-09-29T08:00:00",
            },
            {
                "title": "Story 2",
                "url": "https://news.example/2",
                "snippet": "About story 2",
                "engine": "duckduckgo",
                "published": None,
            },
        ],
    }
    assert fake.params == [{"q": "chips", "format": "json"}]


async def test_duplicates_and_hits_without_a_url_are_dropped():
    fake = FakeSearxng([hit(1), hit(2, url="https://news.example/1"), {"title": "no url"}, hit(3)])
    out = await fake.client().search("chips")
    assert [r["url"] for r in out["results"]] == ["https://news.example/1", "https://news.example/3"]


async def test_max_results_is_capped_at_twenty():
    fake = FakeSearxng([hit(n) for n in range(30)])
    assert len((await fake.client().search("x"))["results"]) == 8
    assert len((await fake.client().search("x", max_results=50))["results"]) == 20
    assert len((await fake.client().search("x", max_results=0))["results"]) == 1


async def test_time_range_is_passed_on():
    fake = FakeSearxng([hit(1)])
    await fake.client().search("chips", time_range="day")
    assert fake.params[0]["time_range"] == "day"


@pytest.mark.parametrize("fake", [FakeSearxng(timeout=True), FakeSearxng(status=502)])
async def test_failures_are_unavailable(fake):
    with pytest.raises(SearchUnavailable):
        await fake.client().search("chips")


async def test_the_tool_turns_failures_into_a_readable_error():
    async with Client(build_mcp(POLICY, web=FakeSearxng(timeout=True).client())) as c:
        res = await c.call_tool("web_search", {"query": "chips"}, meta=KID, raise_on_error=False)
    assert res.is_error and res.content[0].text == "web search is unavailable right now"


async def test_the_tool_returns_results_and_refuses_a_blank_query():
    async with Client(build_mcp(POLICY, web=FakeSearxng([hit(1)]).client())) as c:
        ok = await c.call_tool("web_search", {"query": "chips", "time_range": "week"}, meta=KID)
        blank = await c.call_tool("web_search", {"query": "  "}, meta=KID, raise_on_error=False)
    assert set(ok.structured_content) == {"results", "query", "next"}
    assert ok.structured_content["results"][0]["url"] == "https://news.example/1"
    assert "end with the URL of every result you used" in ok.structured_content["next"]
    assert blank.is_error and "needs a query" in blank.content[0].text


async def test_web_search_without_searxng_says_not_configured():
    async with Client(build_mcp(POLICY)) as c:
        res = await c.call_tool("web_search", {"query": "chips"}, meta=KID, raise_on_error=False)
    assert res.is_error and "web search is not configured" in res.content[0].text
