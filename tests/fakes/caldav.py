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
        base = f"/remote.php/dav/calendars/{user}"
        self.app = Starlette(
            routes=[
                Route(base + "/{slug}/", self._calendar, methods=["MKCALENDAR", "POST", "REPORT"]),
                Route(base + "/{slug}/{name}", self._item, methods=["PUT", "DELETE"]),
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

    def _authorised(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        return base64.b64decode(header[6:]).decode() == f"{self.user}:{self.password}"

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
            component = re.search(r'<c:comp name="(\w+)"', body)[1]
            self.calendars[slug] = FakeCalendar(name, component)
            return Response(status_code=201)
        if cal is None:
            return Response(status_code=404)
        if request.method == "POST":
            who = re.search(r"<d:href>(.*?)</d:href>", body)[1]
            cal.shares[who] = "<oc:read-write/>" in body
            return Response(status_code=200)
        return self._report(slug, cal, re.search(r"<d:sync-token>(.*?)</d:sync-token>", body)[1])

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
        if request.method == "PUT":
            if cal is None:
                return Response(status_code=409)
            body = (await request.body()).decode()
            if not (body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR")):
                return Response(status_code=415)
            if f"BEGIN:{cal.component}\r\n" not in body:
                return Response(status_code=415)
            existed = name in cal.items
            cal.items[name] = body
            self._change(cal, name)
            return Response(status_code=204 if existed else 201, headers={"ETag": etag(body)})
        if cal is None or name not in cal.items:
            return Response(status_code=404)
        del cal.items[name]
        self._change(cal, name)
        return Response(status_code=204)
