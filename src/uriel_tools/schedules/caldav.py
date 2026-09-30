"""Nextcloud CalDAV as the `uriel` account: the calendars the mirror writes, shares and syncs."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from uriel_tools.rag.webdav import NotFound, Unreachable, WebDavError, _raise_for

NS = {"d": "DAV:"}
XML = {"Content-Type": "application/xml; charset=utf-8"}


class CalendarMissing(NotFound):
    """The calendar itself is gone (deleted in Nextcloud, or never made)."""


class InvalidSyncToken(WebDavError):
    """The server no longer knows the sync token: start again with a full sync."""


@dataclass(frozen=True)
class SyncResult:
    token: str | None  # None when the response carried none: keep the old one
    deleted: list[str]  # UIDs (file names without .ics) of items the server reported as 404
    changed: dict[str, str | None]  # UID -> getetag of every other item reported (created or changed)


class CalDav:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self.user = user
        self.root = f"/remote.php/dav/calendars/{quote(user)}/"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), auth=(user, password), transport=transport, timeout=timeout
        )

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kw)
        except httpx.TransportError as e:
            raise Unreachable(type(e).__name__) from e

    async def _xml(self, method: str, url: str, body: str, **headers: str) -> httpx.Response:
        xml = '<?xml version="1.0" encoding="utf-8"?>' + body
        return await self._request(method, url, content=xml.encode(), headers=XML | headers)

    def href(self, slug: str) -> str:
        return f"{self.root}{quote(slug)}/"

    async def make_calendar(self, slug: str, name: str, component: Literal["VEVENT", "VTODO"]) -> str:
        """Create a calendar, or find it already there (405); returns its href."""
        href = self.href(slug)
        r = await self._xml(
            "MKCALENDAR",
            href,
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            f"<d:displayname>{escape(name)}</d:displayname><c:supported-calendar-component-set>"
            f'<c:comp name="{component}"/></c:supported-calendar-component-set></d:prop></d:set>'
            "</c:mkcalendar>",
        )
        if r.status_code != 405:
            _raise_for(r)
        return href

    async def share(self, href: str, with_user: str) -> None:
        """Share a calendar read-write with a Nextcloud user: the POST Nextcloud's Calendar app sends."""
        r = await self._xml(
            "POST",
            href,
            '<oc:share xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><oc:set>'
            f"<d:href>principal:principals/users/{escape(with_user)}</d:href><oc:read-write/>"
            "</oc:set></oc:share>",
        )
        _raise_for(r)

    async def put(self, href: str, uid: str, ics: str) -> str | None:
        """Create or overwrite an item; its new ETag, when the server sends one. Uriel owns its items, so
        there's no If-Match."""
        r = await self._request(
            "PUT", f"{href}{quote(uid)}.ics", content=ics.encode(), headers={"Content-Type": "text/calendar"}
        )
        if r.status_code == 409 and b"no-uid-conflict" in r.content:  # another item already has this UID
            raise WebDavError("WebDAV 409")
        if r.status_code in (404, 409):  # sabre answers 409 when the calendar doesn't exist
            raise CalendarMissing(href)
        _raise_for(r)
        return r.headers.get("etag")

    async def delete(self, href: str, uid: str) -> None:
        r = await self._request("DELETE", f"{href}{quote(uid)}.ics")
        if r.status_code != 404:  # already gone is fine
            _raise_for(r)

    async def sync(self, href: str, token: str | None) -> SyncResult:
        """RFC 6578 sync-collection: what changed since `token` (everything, when None)."""
        r = await self._xml(
            "REPORT",
            href,
            f'<d:sync-collection xmlns:d="DAV:"><d:sync-token>{escape(token or "")}</d:sync-token>'
            "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>",
            Depth="1",
        )
        if r.status_code in (403, 409) and b"valid-sync-token" in r.content:
            raise InvalidSyncToken(href)
        if r.status_code == 404:
            raise CalendarMissing(href)
        _raise_for(r)
        return parse_sync(r.content, href)

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_sync(xml: bytes, href: str) -> SyncResult:
    """Deletions are only the items reported with status 404; an empty body reports nothing."""
    if not xml.strip():
        return SyncResult(None, [], {})
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise WebDavError("malformed sync response") from e
    deleted, changed = [], {}
    for resp in root.findall("d:response", NS):
        item = unquote(resp.findtext("d:href", default="", namespaces=NS))
        status = resp.findtext("d:status", default="", namespaces=NS)  # set only when the item is gone
        folder, _, name = item.rpartition("/")
        # endswith: Nextcloud may be served under a URL prefix the href we sent doesn't carry
        if not (f"{folder}/".endswith(unquote(href)) and name.endswith(".ics")):
            continue
        uid = name.removesuffix(".ics")
        if " 404 " in f"{status} ":
            deleted.append(uid)
        else:
            changed[uid] = resp.findtext("d:propstat/d:prop/d:getetag", default=None, namespaces=NS)
    return SyncResult(root.findtext("d:sync-token", default=None, namespaces=NS), deleted, changed)
