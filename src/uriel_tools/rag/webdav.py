from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import httpx

from uriel_tools.rag.sources import normalise

NS = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
PROPFIND_BODY = (
    b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getetag/><oc:fileid/><d:getcontenttype/><d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)


class WebDavError(Exception):
    pass


class AuthFailed(WebDavError):
    pass


class NotFound(WebDavError):
    pass


class Unreachable(WebDavError):
    pass


class TooLarge(WebDavError):
    pass


class Exists(WebDavError):
    """A PUT that must not replace anything found a file already there."""


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    is_dir: bool
    etag: str
    fileid: int | None
    mime: str
    size: int
    modified: datetime | None


def _raise_for(r: httpx.Response) -> None:
    if r.status_code == 401:
        raise AuthFailed(f"WebDAV {r.status_code}")
    if r.status_code == 404:
        raise NotFound("WebDAV 404")
    if r.status_code >= 500:
        raise Unreachable(f"WebDAV {r.status_code}")
    if r.status_code >= 400:
        raise WebDavError(f"WebDAV {r.status_code}")


def parse_multistatus(xml: bytes, user: str) -> list[RemoteEntry]:
    marker = f"/remote.php/dav/files/{user}"
    entries = []
    for resp in ET.fromstring(xml).findall("d:response", NS):
        href = unquote(resp.findtext("d:href", default="", namespaces=NS))
        rel = href[href.index(marker) + len(marker) :] if marker in href else href
        prop = ET.Element("prop")
        for ps in resp.findall("d:propstat", NS):  # Nextcloud puts missing props in a separate 404 propstat
            if " 200 " in (ps.findtext("d:status", default="", namespaces=NS) + " "):
                prop = ps.find("d:prop", NS)
        fileid = prop.findtext("oc:fileid", default="", namespaces=NS)
        modified = prop.findtext("d:getlastmodified", default="", namespaces=NS)
        entries.append(
            RemoteEntry(
                path=normalise(rel),
                is_dir=prop.find("d:resourcetype/d:collection", NS) is not None,
                etag=prop.findtext("d:getetag", default="", namespaces=NS).strip('"'),
                fileid=int(fileid) if fileid.isdigit() else None,
                mime=prop.findtext("d:getcontenttype", default="", namespaces=NS),
                size=int(prop.findtext("d:getcontentlength", default="0", namespaces=NS) or 0),
                modified=parsedate_to_datetime(modified) if modified else None,
            )
        )
    return entries


class WebDav:
    """Nextcloud WebDAV as one user, authenticated with that user's app password."""

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
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), auth=(user, password), transport=transport, timeout=timeout
        )

    def _url(self, path: str) -> str:
        return quote(f"/remote.php/dav/files/{self.user}{'' if normalise(path) == '/' else normalise(path)}")

    async def _propfind(self, path: str, depth: str) -> list[RemoteEntry]:
        try:
            r = await self._client.request(
                "PROPFIND",
                self._url(path),
                headers={"Depth": depth, "Content-Type": "application/xml"},
                content=PROPFIND_BODY,
            )
        except httpx.TransportError as e:
            raise Unreachable(type(e).__name__) from e
        _raise_for(r)
        return parse_multistatus(r.content, self.user)

    async def stat(self, path: str) -> RemoteEntry:
        return (await self._propfind(path, "0"))[0]

    async def children(self, path: str) -> list[RemoteEntry]:
        return [e for e in await self._propfind(path, "1") if e.path != normalise(path)]

    async def put_new(self, path: str, data: bytes, mime: str = "application/octet-stream") -> None:
        """Create a file, never replace one: with `If-None-Match: *` the server refuses a taken name."""
        try:
            r = await self._client.put(
                self._url(path), content=data, headers={"If-None-Match": "*", "Content-Type": mime}
            )
        except httpx.TransportError as e:
            raise Unreachable(type(e).__name__) from e
        if r.status_code == 412:
            raise Exists(path)
        _raise_for(r)

    async def walk(self, path: str) -> AsyncIterator[RemoteEntry]:
        # Depth 1 per directory: Depth infinity can be disabled on the server.
        pending = [normalise(path)]
        while pending:
            current = pending.pop()
            for entry in await self._propfind(current, "1"):
                if entry.path == current:
                    continue
                if entry.is_dir:
                    pending.append(entry.path)
                else:
                    yield entry

    async def get(self, path: str, max_bytes: int) -> bytes:
        try:
            async with self._client.stream("GET", self._url(path)) as r:
                _raise_for(r)
                buf = bytearray()
                async for part in r.aiter_bytes():
                    buf += part
                    if len(buf) > max_bytes:
                        raise TooLarge(f"larger than {max_bytes} bytes")
                return bytes(buf)
        except httpx.TransportError as e:
            raise Unreachable(type(e).__name__) from e

    async def aclose(self) -> None:
        await self._client.aclose()
