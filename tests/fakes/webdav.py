import base64
import hashlib
import itertools
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

OCS = "/ocs/v2.php/apps/files_sharing/api/v1"


@dataclass
class FakeFile:
    data: bytes
    mime: str
    etag: str
    fileid: int
    modified: datetime


class FakeNextcloud:
    """In-memory Nextcloud WebDAV (PROPFIND depth 0/1 and GET, basic auth) and the OCS share listing, plus an
    unauthenticated /_fake control API so e2e tests can add and remove files and shares.

    Shares are listed as given for every user; files of a share live directly under its path in the
    recipient's tree, the way Nextcloud mounts them."""

    def __init__(self, passwords: dict[str, str], shares: list[dict] | None = None):
        self.passwords = dict(passwords)
        self.shares: list[dict] = list(shares or [])
        self.files: dict[tuple[str, str], FakeFile] = {}
        self.gets: list[tuple[str, str]] = []
        self._ids = itertools.count(1000)
        self.app = Starlette(
            routes=[
                Route("/remote.php/dav/files/{user}", self._dav, methods=["PROPFIND"]),
                Route(
                    "/remote.php/dav/files/{user}/{path:path}", self._dav, methods=["PROPFIND", "GET", "PUT"]
                ),
                Route("/_fake/files/{user}/{path:path}", self._control, methods=["PUT", "DELETE"]),
                Route(OCS + "/shares", self._shares, methods=["GET"]),
                Route(OCS + "/shares/pending", self._pending, methods=["GET"]),
                Route("/_fake/shares", self._control_shares, methods=["POST"]),
                Route("/_fake/shares/{id}", self._control_shares, methods=["DELETE"]),
            ]
        )

    def put(self, user: str, path: str, data: bytes, mime: str = "application/octet-stream") -> FakeFile:
        path = "/" + path.strip("/")
        old = self.files.get((user, path))
        f = FakeFile(
            data,
            mime,
            uuid.uuid4().hex,
            old.fileid if old else next(self._ids),
            datetime.now(UTC).replace(microsecond=0),
        )
        self.files[(user, path)] = f
        return f

    def delete(self, user: str, path: str) -> None:
        path = "/" + path.strip("/")
        doomed = [
            k
            for k in self.files
            if k[0] == user and (k[1] == path or k[1].startswith(path.rstrip("/") + "/"))
        ]
        for key in doomed:
            del self.files[key]

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def _caller(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return None
        name, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return name if self.passwords.get(name) == password else None

    async def _shares(self, request: Request) -> Response:
        if self._caller(request) is None:
            return Response(status_code=401)
        return JSONResponse({"ocs": {"meta": {"statuscode": 200}, "data": self.shares}})

    async def _pending(self, request: Request) -> Response:
        if self._caller(request) is None:
            return Response(status_code=401)
        return JSONResponse({"ocs": {"meta": {"statuscode": 200}, "data": []}})

    async def _control_shares(self, request: Request) -> Response:
        if request.method == "POST":
            self.shares.append(await request.json())
            return JSONResponse({"shares": len(self.shares)})
        self.shares = [s for s in self.shares if s.get("id") != request.path_params["id"]]
        return Response(status_code=204)

    def _authorised(self, request: Request, user: str) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        name, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return name == user and self.passwords.get(user) == password

    async def _dav(self, request: Request) -> Response:
        user = request.path_params["user"]
        path = "/" + request.path_params.get("path", "").strip("/")
        if not self._authorised(request, user):
            return Response(status_code=401)
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and (user, path) in self.files:
                return Response(status_code=412)
            existed = (user, path) in self.files
            self.put(
                user,
                path,
                await request.body(),
                request.headers.get("content-type", "application/octet-stream"),
            )
            return Response(status_code=204 if existed else 201)
        if request.method == "GET":
            f = self.files.get((user, path))
            if f is None:
                return Response(status_code=404)
            self.gets.append((user, path))
            return Response(f.data, media_type=f.mime)
        entries = self._entries(user, path, request.headers.get("depth", "1"))
        if entries is None:
            return Response(status_code=404)
        body = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            + "".join(self._response(user, p, f) for p, f in entries)
            + "</d:multistatus>"
        )
        return Response(body, status_code=207, media_type="application/xml")

    async def _control(self, request: Request) -> Response:
        user, path = request.path_params["user"], request.path_params["path"]
        if request.method == "PUT":
            mime = request.headers.get("content-type", "application/octet-stream")
            f = self.put(user, path, await request.body(), mime)
            return JSONResponse({"fileid": f.fileid, "etag": f.etag})
        self.delete(user, path)
        return Response(status_code=204)

    def _entries(self, user: str, path: str, depth: str) -> list[tuple[str, FakeFile | None]] | None:
        """[(path, file or None for a directory)], or None when nothing is there."""
        if (user, path) in self.files:
            return [(path, self.files[(user, path)])]
        prefix = "" if path == "/" else path
        below = [(p, f) for (u, p), f in self.files.items() if u == user and p.startswith(prefix + "/")]
        if not below and path != "/":
            return None
        out: list[tuple[str, FakeFile | None]] = [(path, None)]
        if depth != "0":
            children: dict[str, FakeFile | None] = {}
            for p, f in below:
                rest = p[len(prefix) + 1 :]
                head = rest.split("/", 1)[0]
                children[f"{prefix}/{head}"] = None if "/" in rest else f
            out += sorted(children.items())
        return out

    def _response(self, user: str, path: str, f: FakeFile | None) -> str:
        href = quote(f"/remote.php/dav/files/{user}{'' if path == '/' else path}") + (
            "/" if f is None else ""
        )
        if f is None:
            below = sorted(
                v.etag
                for (u, p), v in self.files.items()
                if u == user and p.startswith(path.rstrip("/") + "/")
            )
            etag = hashlib.sha256("".join(below).encode()).hexdigest()[:16]
            props = f'<d:resourcetype><d:collection/></d:resourcetype><d:getetag>"{etag}"</d:getetag>'
        else:
            props = (
                f'<d:resourcetype/><d:getetag>"{f.etag}"</d:getetag><oc:fileid>{f.fileid}</oc:fileid>'
                f"<d:getcontenttype>{escape(f.mime)}</d:getcontenttype>"
                f"<d:getcontentlength>{len(f.data)}</d:getcontentlength>"
                f"<d:getlastmodified>{format_datetime(f.modified, usegmt=True)}</d:getlastmodified>"
            )
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )


def create_app_from_env() -> Starlette:
    """For the e2e compose service: FAKE_NC_PASSWORDS="user:pw,user2:pw", FAKE_NC_SHARES='[{...}]'."""
    pairs = [p.split(":", 1) for p in os.environ.get("FAKE_NC_PASSWORDS", "").split(",") if ":" in p]
    return FakeNextcloud(dict(pairs), json.loads(os.environ.get("FAKE_NC_SHARES", "[]"))).app
