import httpx
import pytest

from uriel_tools.gating import Caller
from uriel_tools.rag.shares import Mount, Shares, mounts_from
from uriel_tools.rag.webdav import AuthFailed


def share(path, owner, kind=0, to="uriel", name=None, id="1"):
    return {
        "id": id,
        "share_type": kind,
        "share_with": to,
        "uid_owner": owner,
        "displayname_owner": name or owner,
        "path": path,
    }


def test_user_share_is_private_to_its_owner():
    assert mounts_from([share("/Documents (2)", "anthony-headband")], "uriel-") == [
        Mount("/Documents (2)", ("anthony-headband",), "anthony-headband", ())
    ]


def test_prefixed_group_share_opens_the_folder_to_that_group():
    [m] = mounts_from([share("/Scuola", "authelia-19e1", kind=1, to="uriel-family", name="Lil-B")], "uriel-")
    assert (m.owners, m.shared_by, m.share_with) == (("authelia-19e1",), "Lil-B", ("family",))


def test_unprefixed_group_shares_and_other_share_types_are_ignored():
    shares = [
        share("/Family photos", "a", kind=1, to="family"),  # shared with the family for other reasons
        share("/Link", "a", kind=3, to=""),
        share("/Remote", "a", kind=6, to="x@elsewhere"),
    ]
    assert mounts_from(shares, "uriel-") == []


def test_shares_of_one_mount_merge_owners_and_groups():
    # Nextcloud mounts a node once however many shares bring it: to uriel, to uriel-family, and a reshare.
    shares = [
        share("/Casa", "anthony-headband", id="1"),
        share("/Casa", "anthony-headband", kind=1, to="uriel-family", id="2"),
        share("/Casa", "authelia-19e1", name="Lil-B", id="3"),
    ]
    assert mounts_from(shares, "uriel-") == [
        Mount("/Casa", ("anthony-headband", "authelia-19e1"), "Lil-B, anthony-headband", ("family",))
    ]


def test_caller_nextcloud_ids_include_the_sociallogin_account():
    assert Caller("lil-b", frozenset(), None, "19e1").nextcloud_ids == ["lil-b", "authelia-19e1"]
    assert Caller("anthony-headband", frozenset(), None).nextcloud_ids == ["anthony-headband"]
    assert Caller(None, frozenset(), None).nextcloud_ids == []


def ocs_app(pending: list[dict], shared: list[dict], status: int = 200):
    accepted = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["ocs-apirequest"] == "true"
        if status != 200:
            return httpx.Response(status)
        path = request.url.path.removeprefix("/ocs/v2.php/apps/files_sharing/api/v1")
        if request.method == "POST" and path.startswith("/shares/pending/"):
            accepted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"ocs": {"data": []}})
        if path == "/shares/pending":
            return httpx.Response(200, json={"ocs": {"data": pending}})
        assert path == "/shares" and request.url.params["shared_with_me"] == "true"
        return httpx.Response(200, json={"ocs": {"data": shared}})

    return httpx.MockTransport(handler), accepted


async def test_client_accepts_pending_shares_then_lists():
    transport, accepted = ocs_app([{"id": 7}], [share("/D", "a")])
    client = Shares("http://nc", "uriel", "pw", transport=transport)
    assert [s["path"] for s in await client.shared_with_me()] == ["/D"]
    assert accepted == ["7"]


async def test_client_raises_auth_failed_on_401():
    transport, _ = ocs_app([], [], status=401)
    with pytest.raises(AuthFailed):
        await Shares("http://nc", "uriel", "bad", transport=transport).shared_with_me()


def test_caller_from_reads_the_optional_sub():
    from types import SimpleNamespace

    from pydantic import BaseModel, ConfigDict

    from uriel_tools.gating import caller_from

    class Meta(BaseModel):
        model_config = ConfigDict(extra="allow")

    def ctx(**meta):
        return SimpleNamespace(request_context=SimpleNamespace(meta=Meta(**meta)))

    assert caller_from(ctx(user="lil-b", groups=["family"], sub="19e1")).nextcloud_ids == [
        "lil-b",
        "authelia-19e1",
    ]
    assert caller_from(ctx(user="anthony-headband", groups=[])).sub is None
