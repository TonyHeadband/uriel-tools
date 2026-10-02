import base64
import hashlib
import itertools
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

TOKEN = "http://sabre.io/ns/sync/"


def etag(ics: str) -> str:
    """Like Nextcloud's: the quoted md5 of the stored text."""
    return f'"{hashlib.md5(ics.encode()).hexdigest()}"'


@dataclass
class FakeCalendar:
    name: str
    component: str
    items: dict[str, str] = field(default_factory=dict)  # file name -> iCalendar text
    shares: dict[str, bool] = field(default_factory=dict)  # principal href -> read-write
    changes: list[tuple[int, str]] = field(default_factory=list)  # (sync token, file name)
    owner: str = "uriel"
    sharees: tuple[str, ...] = ()  # "users/uriel", "groups/uriel-family"
    read_only: bool = False
    components: tuple[str, ...] = ()  # empty: just `component`


class FakeCalDav:
    """In-memory Nextcloud CalDAV for one account: MKCALENDAR, the sharing POST, PUT, DELETE and the
    sync-collection REPORT, with sabre-style sync tokens. `fail` makes a method answer with a status, and
    `empty_report` makes the REPORT answer an empty multistatus without a token."""

    def __init__(self, user: str = "uriel", password: str = "pw"):
        self.user, self.password = user, password
        self.calendars: dict[str, FakeCalendar] = {}
        self.fail: dict[str, int] = {}
        self.empty_report = False
        self.report_body: str | None = None  # raw REPORT answer, e.g. a malformed one
        self.conflict_body = ""  # body sent with a `fail` status
        self.requests: list[tuple[str, str]] = []
        self._tokens = itertools.count(1)
        self.token = 0
        self.expanded: dict[str, str] = {}  # item href -> text the REPORT returns instead of the stored text
        base = f"/remote.php/dav/calendars/{user}"
        self.app = Starlette(
            routes=[
                Route(base + "/", self._home, methods=["PROPFIND"]),
                Route(base + "/{slug}/", self._calendar, methods=["MKCALENDAR", "POST", "REPORT"]),
                Route(base + "/{slug}/{name}", self._item, methods=["PUT", "DELETE", "GET"]),
            ]
        )

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def _change(self, cal: FakeCalendar, name: str) -> None:
        self.token = next(self._tokens)
        cal.changes.append((self.token, name))

    # What a person does in Nextcloud.
    def person_deletes(self, slug: str, uid: str) -> None:
        cal = self.calendars[slug]
        del cal.items[f"{uid}.ics"]
        self._change(cal, f"{uid}.ics")

    def person_edits(self, slug: str, uid: str, ics: str) -> None:
        cal = self.calendars[slug]
        cal.items[f"{uid}.ics"] = ics
        self._change(cal, f"{uid}.ics")

    def person_deletes_calendar(self, slug: str) -> None:
        del self.calendars[slug]

    def add_calendar(
        self,
        slug: str,
        name: str,
        *,
        owner: str,
        components: tuple[str, ...] = ("VEVENT", "VTODO"),
        sharees: tuple[str, ...] = (),
        read_only: bool = False,
    ) -> str:
        """A calendar as Nextcloud lists it in uriel's home: made by `owner` and shared with `sharees`."""
        self.calendars[slug] = FakeCalendar(
            name, components[0], owner=owner, sharees=sharees, read_only=read_only, components=components
        )
        return f"/remote.php/dav/calendars/{self.user}/{slug}/"

    def item(self, href: str) -> str | None:
        slug, name = href.rstrip("/").split("/")[-2:]
        cal = self.calendars.get(slug)
        return cal.items.get(name) if cal else None

    def _authorised(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        return base64.b64decode(header[6:]).decode() == f"{self.user}:{self.password}"

    async def _home(self, request: Request) -> Response:
        self.requests.append(("PROPFIND", ""))
        if not self._authorised(request):
            return Response(status_code=401)
        if "PROPFIND" in self.fail:
            return Response(status_code=self.fail["PROPFIND"])
        base = f"/remote.php/dav/calendars/{self.user}/"
        parts = [
            f"<d:response><d:href>{base}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>"
            "</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        ]
        for slug, cal in self.calendars.items():
            comps = "".join(f'<c:comp name="{c}"/>' for c in (cal.components or (cal.component,)))
            invite = "".join(
                f"<oc:user><d:href>principal:principals/{s}</d:href><oc:access><oc:read-write/></oc:access>"
                "</oc:user>"
                for s in cal.sharees
            )
            parts.append(
                f"<d:response><d:href>{base}{quote(slug)}/</d:href><d:propstat><d:prop>"
                "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                f"<d:displayname>{cal.name}</d:displayname>"
                f"<oc:owner-principal>principals/users/{cal.owner}</oc:owner-principal>"
                f"<oc:read-only>{'true' if cal.read_only else 'false'}</oc:read-only>"
                f"<oc:invite>{invite}</oc:invite>"
                f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            )
        xml = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(parts) + "</d:multistatus>"
        )
        return Response(xml, status_code=207, media_type="application/xml")

    async def _calendar(self, request: Request) -> Response:
        slug = request.path_params["slug"]
        self.requests.append((request.method, slug))
        if not self._authorised(request):
            return Response(status_code=401)
        if request.method in self.fail:
            return Response(self.conflict_body, status_code=self.fail[request.method])
        body = (await request.body()).decode()
        cal = self.calendars.get(slug)
        if request.method == "MKCALENDAR":
            if cal is not None:
                return Response(status_code=405)
            name = re.search(r"<d:displayname>(.*?)</d:displayname>", body)[1]
            components = tuple(re.findall(r'<c:comp name="(\w+)"', body))
            self.calendars[slug] = FakeCalendar(name, components[0], components=components)
            return Response(status_code=201)
        if cal is None:
            return Response(status_code=404)
        if request.method == "POST":
            who = re.search(r"<d:href>(.*?)</d:href>", body)[1]
            cal.shares[who] = "<oc:read-write/>" in body
            return Response(status_code=200)
        if "calendar-query" in body:
            wanted = re.findall(r'<c:comp-filter name="(V(?:EVENT|TODO))"', body)
            return self._query(slug, cal, wanted[0] if wanted else "VEVENT")
        return self._report(slug, cal, re.search(r"<d:sync-token>(.*?)</d:sync-token>", body)[1])

    def _query(self, slug: str, cal: FakeCalendar, component: str) -> Response:
        """Every stored item holding `component`: the real server filters by time and expands recurrences;
        tests stand in for expansion with `expanded`."""
        href = f"/remote.php/dav/calendars/{self.user}/{slug}/"
        parts = []
        for name, text in sorted(cal.items.items()):
            if f"BEGIN:{component}\r\n" not in text:
                continue
            shown = self.expanded.get(f"{href}{name}", text)
            data = shown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(
                f"<d:response><d:href>{href}{quote(name)}</d:href><d:propstat><d:prop>"
                f"<d:getetag>{etag(text)}</d:getetag><c:calendar-data>{data}</c:calendar-data>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            )
        xml = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            + "".join(parts)
            + "</d:multistatus>"
        )
        return Response(xml, status_code=207, media_type="application/xml")

    def _report(self, slug: str, cal: FakeCalendar, token: str) -> Response:
        if self.report_body is not None:
            return Response(self.report_body, status_code=207, media_type="application/xml")
        if self.empty_report:
            return Response('<d:multistatus xmlns:d="DAV:"/>', status_code=207, media_type="application/xml")
        since = 0
        if token:
            if not token.startswith(TOKEN) or int(token[len(TOKEN) :]) > self.token:
                error = '<d:error xmlns:d="DAV:"><d:valid-sync-token/></d:error>'
                return Response(error, status_code=403, media_type="application/xml")
            since = int(token[len(TOKEN) :])
        names = sorted({n for t, n in cal.changes if t > since}) if token else sorted(cal.items)
        href = f"/remote.php/dav/calendars/{self.user}/{slug}/"
        parts = []
        for name in names:
            if name in cal.items:
                parts.append(
                    f"<d:response><d:href>{href}{quote(name)}</d:href><d:propstat><d:prop>"
                    f"<d:getetag>{etag(cal.items[name])}</d:getetag></d:prop>"
                    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                )
            else:
                parts.append(
                    f"<d:response><d:href>{href}{quote(name)}</d:href>"
                    "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
                )
        xml = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            + "".join(parts)
            + f"<d:sync-token>{TOKEN}{self.token}</d:sync-token></d:multistatus>"
        )
        return Response(xml, status_code=207, media_type="application/xml")

    async def _item(self, request: Request) -> Response:
        slug, name = request.path_params["slug"], request.path_params["name"]
        self.requests.append((request.method, f"{slug}/{name}"))
        if not self._authorised(request):
            return Response(status_code=401)
        if request.method in self.fail:
            return Response(self.conflict_body, status_code=self.fail[request.method])
        cal = self.calendars.get(slug)
        if request.method == "GET":
            if cal is None or name not in cal.items:
                return Response(status_code=404)
            text = cal.items[name]
            return Response(text, media_type="text/calendar", headers={"ETag": etag(text)})
        if request.method == "PUT":
            if cal is None:
                return Response(status_code=409)
            if cal.read_only:
                return Response(status_code=403)
            body = (await request.body()).decode()
            if not (body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR")):
                return Response(status_code=415)
            if not any(f"BEGIN:{c}\r\n" in body for c in (cal.components or (cal.component,))):
                return Response(status_code=415)
            existed = name in cal.items
            if not self._precondition(request, cal.items.get(name)):
                return Response(status_code=412)
            cal.items[name] = body
            self._change(cal, name)
            return Response(status_code=204 if existed else 201, headers={"ETag": etag(body)})
        if cal is None or name not in cal.items:
            return Response(status_code=404)
        if cal.read_only:
            return Response(status_code=403)
        if not self._precondition(request, cal.items[name]):
            return Response(status_code=412)
        del cal.items[name]
        self._change(cal, name)
        return Response(status_code=204)

    @staticmethod
    def _precondition(request: Request, current: str | None) -> bool:
        match, none_match = request.headers.get("if-match"), request.headers.get("if-none-match")
        if none_match == "*" and current is not None:
            return False
        return match is None or (current is not None and match == etag(current))
