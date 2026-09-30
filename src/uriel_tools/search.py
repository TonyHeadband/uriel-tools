"""Web search through the family's SearXNG. Results only: no page is fetched."""

from typing import Literal

import httpx

TimeRange = Literal["day", "week", "month", "year"]
MAX_RESULTS = 20


class SearchUnavailable(Exception):
    pass


class WebSearch:
    def __init__(
        self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0
    ):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=timeout)

    async def search(self, query: str, time_range: TimeRange | None = None, max_results: int = 8) -> dict:
        params = {"q": query, "format": "json"}
        if time_range:
            params["time_range"] = time_range
        try:
            r = await self._client.get("/search", params=params)
            r.raise_for_status()
            found = r.json().get("results", [])
        except (httpx.HTTPError, ValueError) as e:  # ValueError: a body that isn't JSON
            raise SearchUnavailable(f"{type(e).__name__}: {e}") from e
        limit = max(1, min(max_results, MAX_RESULTS))
        results, seen = [], set()
        for hit in found:
            url = hit.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "title": hit.get("title") or url,
                    "url": url,
                    "snippet": hit.get("content") or "",
                    "engine": hit.get("engine") or "",
                    "published": hit.get("publishedDate"),
                }
            )
            if len(results) == limit:
                break
        return {"results": results, "query": query}

    async def aclose(self) -> None:
        await self._client.aclose()
