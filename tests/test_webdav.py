import httpx
import pytest

from tests.fakes.webdav import FakeNextcloud
from uriel_tools.rag.webdav import (
    AuthFailed,
    NotFound,
    TooLarge,
    Unreachable,
    WebDav,
    WebDavError,
    parse_multistatus,
)


@pytest.fixture
def nc():
    fake = FakeNextcloud({"alice": "pw"})
    fake.put("alice", "/Documents/a.txt", b"hello", "text/plain")
    fake.put("alice", "/Documents/Casa/b.pdf", b"%PDF-", "application/pdf")
    fake.put("alice", "/Photos/c.jpg", b"jpg", "image/jpeg")
    return fake


def dav(nc, password="pw"):
    return WebDav("http://nc", "alice", password, transport=nc.transport())


async def test_stat_reads_file_properties(nc):
    e = await dav(nc).stat("/Documents/a.txt")
    f = nc.files[("alice", "/Documents/a.txt")]
    assert (e.path, e.is_dir, e.etag, e.fileid, e.mime, e.size) == (
        "/Documents/a.txt",
        False,
        f.etag,
        f.fileid,
        "text/plain",
        5,
    )
    assert e.modified == f.modified


async def test_stat_on_a_directory(nc):
    e = await dav(nc).stat("/Documents")
    assert e.is_dir and e.path == "/Documents"


async def test_walk_yields_files_recursively(nc):
    assert sorted([e.path async for e in dav(nc).walk("/Documents")]) == [
        "/Documents/Casa/b.pdf",
        "/Documents/a.txt",
    ]


async def test_get_and_size_cap(nc):
    assert await dav(nc).get("/Documents/a.txt", max_bytes=10) == b"hello"
    with pytest.raises(TooLarge):
        await dav(nc).get("/Documents/a.txt", max_bytes=4)


async def test_errors_are_classified(nc):
    with pytest.raises(AuthFailed):
        await dav(nc, password="wrong").stat("/Documents")
    with pytest.raises(NotFound):
        await dav(nc).stat("/Nope")

    forbidden = httpx.MockTransport(lambda r: httpx.Response(403))
    with pytest.raises(WebDavError) as err:
        await WebDav("http://nc", "alice", "pw", transport=forbidden).stat("/")
    assert not isinstance(err.value, AuthFailed)  # an ACL/antivirus block on one file, not a bad password

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(Unreachable):
        await WebDav("http://nc", "alice", "pw", transport=httpx.MockTransport(down)).stat("/")
    busy = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(Unreachable):
        await WebDav("http://nc", "alice", "pw", transport=busy).stat("/")
    odd = httpx.MockTransport(lambda r: httpx.Response(400))
    with pytest.raises(WebDavError):
        await WebDav("http://nc", "alice", "pw", transport=odd).stat("/")


async def test_paths_with_spaces_and_accents_round_trip(nc):
    path = "/Documents/Scuola 2026/pagella è 100%.pdf"
    nc.put("alice", path, b"x", "application/pdf")
    client = dav(nc)
    assert path in [e.path async for e in client.walk("/Documents")]
    assert await client.get(path, max_bytes=10) == b"x"


def test_parse_multistatus_handles_subpaths_and_404_propstats():
    xml = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
 <d:response><d:href>/nextcloud/remote.php/dav/files/bob/Docs/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype><d:getetag>"d1"</d:getetag></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  <d:propstat><d:prop><d:getcontentlength/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
 </d:response>
 <d:response><d:href>/nextcloud/remote.php/dav/files/bob/Docs/r%C3%A9sum%C3%A9.odt</d:href>
  <d:propstat><d:prop><d:resourcetype/><d:getetag>"f1"</d:getetag><oc:fileid>42</oc:fileid>
   <d:getcontenttype>application/vnd.oasis.opendocument.text</d:getcontenttype><d:getcontentlength>7</d:getcontentlength>
   <d:getlastmodified>Mon, 02 Mar 2026 10:00:00 GMT</d:getlastmodified></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""
    d, f = parse_multistatus(xml, "bob")
    assert (d.path, d.is_dir, d.etag, d.size) == ("/Docs", True, "d1", 0)
    assert (f.path, f.fileid, f.size, f.etag) == ("/Docs/résumé.odt", 42, 7, "f1")
    assert f.modified.year == 2026
