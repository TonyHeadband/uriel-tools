import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from uriel_tools.gating import Caller

log = logging.getLogger(__name__)

TITLE_MAX = 120
TEXT_MAX = 4000  # per section; the model writes these, so bound what one call can post
FROM_CHAT = "from-chat"


class TrackerError(Exception):
    pass


@dataclass(frozen=True)
class Draft:
    title: str
    labels: list[str]
    body: str


def draft_for(caller: Caller, title: str, kind: str, description: str, what_happened: str | None) -> Draft:
    sections = [description.strip()[:TEXT_MAX]]
    if what_happened and what_happened.strip():
        sections.append(f"## What happened\n\n{what_happened.strip()[:TEXT_MAX]}")
    at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    sections.append(
        f"---\nFiled from the Uriel chat by **{caller.user or 'unknown'}** on {at} "
        f"(request `{caller.request_id or '-'}`)."
    )
    return Draft(title.strip()[:TITLE_MAX], [f"kind/{kind}", FROM_CHAT], "\n\n".join(sections))


class Forgejo:
    """Files issues on one repository as the bot account the token belongs to."""

    def __init__(
        self,
        base_url: str,
        token: str,
        repo: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ):
        self._repo = f"/api/v1/repos/{repo.strip('/')}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"token {token}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self._client.request(method, f"{self._repo}{path}", **kw)
        except httpx.HTTPError as e:
            raise TrackerError(f"{method} {path}: {e!r}") from e
        if r.status_code >= 400:
            raise TrackerError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r

    async def _label_ids(self, names: list[str]) -> list[int]:
        r = await self._request("GET", "/labels", params={"limit": 50})
        known = {label["name"]: label["id"] for label in r.json()}
        missing = [n for n in names if n not in known]
        if missing:
            log.warning("issue labels missing on the tracker, filing without them: %s", missing)
        return [known[n] for n in names if n in known]

    async def create(self, draft: Draft) -> dict:
        labels = await self._label_ids(draft.labels)
        r = await self._request(
            "POST", "/issues", json={"title": draft.title, "body": draft.body, "labels": labels}
        )
        issue = r.json()
        return {"filed": True, "number": issue["number"], "url": issue["html_url"]}
