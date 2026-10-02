"""Nextcloud CalDAV as the `uriel` account: the mirror's own calendars, and those people share with it."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from uriel_tools.rag.webdav import NotFound, Unreachable, WebDavError, _raise_for

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "oc": "http://owncloud.org/ns"}
_PRINCIPAL = re.compile(r"principals/(users|groups)/([^/]+)")
XML = {"Content-Type": "application/xml; charset=utf-8"}


class CalendarMissing(NotFound):
    """The calendar itself is gone (deleted in Nextcloud, or never made)."""


class InvalidSyncToken(WebDavError):
    """The server no longer knows the sync token: start again with a full sync."""


class Changed(WebDavError):
    """The item changed (or appeared) since it was read: 412 on If-Match / If-None-Match."""


@dataclass(frozen=True)
class CalendarInfo:
    href: str
    name: str
    owner: str | None  # the Nextcloud uid from oc:owner-principal
    read_only: bool
    sharees: frozenset[str]  # "users/<uid>" and "groups/<gid>" from oc:invite
    components: frozenset[str]  # VEVENT, VTODO


@dataclass(frozen=True)
class Item:
    href: str
    etag: str | None
    ics: str


@dataclass(frozen=True)
class SyncResult:
    token: str | None  # None when the response carried none: keep the old one
    deleted: list[str]  # UIDs (file names without .ics) of items the server reported as 404
    changed: dict[str, str | None]  # UID -> getetag of every other item reported (created or changed)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


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

    async def make_calendar(
        self, slug: str, name: str, component: Literal["VEVENT", "VTODO"], *, also: tuple[str, ...] = ()
    ) -> str:
        """Create a calendar, or find it already there (405); returns its href. `also` adds components."""
        href = self.href(slug)
        comps = "".join(f'<c:comp name="{c}"/>' for c in (component, *also))
        r = await self._xml(
            "MKCALENDAR",
            href,
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            f"<d:displayname>{escape(name)}</d:displayname><c:supported-calendar-component-set>"
            f"{comps}</c:supported-calendar-component-set></d:prop></d:set>"
            "</c:mkcalendar>",
        )
        if r.status_code != 405:
            _raise_for(r)
        return href

    async def share(self, href: str, with_user: str) -> None:
        """Share a calendar read-write with a Nextcloud user: the POST Nextcloud's Calendar app sends."""
        await self._share(href, f"users/{with_user}")

    async def share_with_group(self, href: str, group: str) -> None:
        """Share a calendar read-write with a Nextcloud group."""
        await self._share(href, f"groups/{group}")

    async def _share(self, href: str, principal: str) -> None:
        r = await self._xml(
            "POST",
            href,
            '<oc:share xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><oc:set>'
            f"<d:href>principal:principals/{escape(principal)}</d:href><oc:read-write/>"
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

    async def calendars(self) -> list[CalendarInfo]:
        """Every calendar in uriel's home: its own, and those shared with it (by user or through a group)."""
        r = await self._xml(
            "PROPFIND",
            self.root,
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:resourcetype/><d:displayname/><oc:owner-principal/><oc:read-only/><oc:invite/>"
            "<c:supported-calendar-component-set/></d:prop></d:propfind>",
            Depth="1",
        )
        _raise_for(r)
        return parse_calendars(r.content)

    async def _query(self, href: str, filter_: str, data: str = "<c:calendar-data/>") -> list[Item]:
        r = await self._xml(
            "REPORT",
            href,
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<d:prop><d:getetag/>{data}</d:prop><c:filter><c:comp-filter name="VCALENDAR">{filter_}'
            "</c:comp-filter></c:filter></c:calendar-query>",
            Depth="1",
        )
        if r.status_code == 404:
            raise CalendarMissing(href)
        _raise_for(r)
        return parse_items(r.content)

    async def events(self, href: str, start: datetime, end: datetime) -> list[Item]:
        """Events overlapping [start, end), each recurrence expanded by the server into its own VEVENT with a
        RECURRENCE-ID, in UTC."""
        span = f'start="{_stamp(start)}" end="{_stamp(end)}"'
        return await self._query(
            href,
            f'<c:comp-filter name="VEVENT"><c:time-range {span}/></c:comp-filter>',
            f"<c:calendar-data><c:expand {span}/></c:calendar-data>",
        )

    async def open_tasks(self, href: str) -> list[Item]:
        """Tasks not completed yet; the caller filters by due date."""
        return await self._query(
            href,
            '<c:comp-filter name="VTODO"><c:prop-filter name="COMPLETED"><c:is-not-defined/></c:prop-filter>'
            "</c:comp-filter>",
        )

    async def get(self, item_href: str) -> Item | None:
        r = await self._request("GET", item_href)
        if r.status_code == 404:
            return None
        _raise_for(r)
        return Item(item_href, r.headers.get("etag"), r.text)

    async def write(
        self, item_href: str, ics: str, *, etag: str | None = None, new: bool = False
    ) -> str | None:
        """PUT an item someone may also edit: with `etag` only if unchanged since read, with `new` only if
        nothing is there yet. Its new ETag, when the server sends one."""
        headers = {"Content-Type": "text/calendar"}
        if etag:
            headers["If-Match"] = etag
        if new:
            headers["If-None-Match"] = "*"
        r = await self._request("PUT", item_href, content=ics.encode(), headers=headers)
        if r.status_code == 412:
            raise Changed(item_href)
        _raise_for(r)
        return r.headers.get("etag")

    async def remove(self, item_href: str, *, etag: str | None = None) -> None:
        r = await self._request("DELETE", item_href, headers={"If-Match": etag} if etag else {})
        if r.status_code == 412:
            raise Changed(item_href)
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


def _multistatus(xml: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml)
    except ET.ParseError as e:
        raise WebDavError("malformed WebDAV response") from e


def _ok_props(resp: ET.Element) -> ET.Element | None:
    """The prop element of the 200 propstat: Nextcloud lists properties it lacks in a separate 404 one."""
    for propstat in resp.findall("d:propstat", NS):
        if " 200 " in f"{propstat.findtext('d:status', default='', namespaces=NS)} ":
            return propstat.find("d:prop", NS)
    return None


def parse_calendars(xml: bytes) -> list[CalendarInfo]:
    found = []
    for resp in _multistatus(xml).findall("d:response", NS):
        prop = _ok_props(resp)
        if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
            continue
        owner = _PRINCIPAL.search(prop.findtext("oc:owner-principal", default="", namespaces=NS))
        sharees = set()
        for href in prop.iterfind("oc:invite/oc:user/d:href", NS):
            if m := _PRINCIPAL.search(href.text or ""):
                sharees.add(f"{m[1]}/{unquote(m[2])}")
        comps = {c.get("name") for c in prop.iterfind("c:supported-calendar-component-set/c:comp", NS)}
        found.append(
            CalendarInfo(
                href=unquote(resp.findtext("d:href", default="", namespaces=NS)),
                name=prop.findtext("d:displayname", default="", namespaces=NS),
                owner=unquote(owner[2]) if owner and owner[1] == "users" else None,
                read_only=prop.findtext("oc:read-only", default="", namespaces=NS).strip().lower()
                in ("1", "true"),
                sharees=frozenset(sharees),
                components=frozenset(comps - {None}) or frozenset({"VEVENT", "VTODO"}),
            )
        )
    return found


def parse_items(xml: bytes) -> list[Item]:
    items = []
    for resp in _multistatus(xml).findall("d:response", NS):
        prop = _ok_props(resp)
        if prop is None:
            continue
        data = prop.findtext("c:calendar-data", default="", namespaces=NS)
        if not data.strip():
            continue
        # XML parsing turns the CRLF iCalendar needs into LF; put it back.
        text = data.replace("\r\n", "\n").replace("\n", "\r\n")
        items.append(
            Item(
                unquote(resp.findtext("d:href", default="", namespaces=NS)),
                prop.findtext("d:getetag", default=None, namespaces=NS),
                text,
            )
        )
    return items
