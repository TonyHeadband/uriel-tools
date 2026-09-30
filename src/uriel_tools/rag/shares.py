import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

from uriel_tools.rag.sources import normalise
from uriel_tools.rag.webdav import AuthFailed, Unreachable, WebDavError

log = logging.getLogger("uriel_tools.shares")

OCS = "/ocs/v2.php/apps/files_sharing/api/v1"
USER_SHARE, GROUP_SHARE = 0, 1
CREATE = 4  # Nextcloud share permission bits: 1 read, 2 update, 4 create, 8 delete, 16 share
# A person's folder of personal items; a second one mounts as "Uriel (2)" in uriel's tree.
PERSONAL = re.compile(r"Uriel( \(\d+\))?", re.IGNORECASE)


@dataclass(frozen=True)
class Mount:
    """A folder or file in uriel's tree that one or more shares put there."""

    path: str
    owners: tuple[str, ...]  # Nextcloud uids of the sharers
    shared_by: str  # their display names, for citing whose folder a hit came from
    share_with: tuple[str, ...]  # Authelia groups that may also search it
    can_create: bool = False  # edit_ files may be saved here
    personal: bool = False  # someone's `Uriel` folder: personal items, used only for them, never indexed


def mounts_from(shares: list[dict], group_prefix: str) -> list[Mount]:
    """Fold the `shared_with_me` listing into mounts.

    Nextcloud mounts a node once even when it arrives through several shares, so shares with the same path
    merge. A group share counts only when the group has the prefix (`uriel-family` -> `family`); other group
    shares and other share types (links, federation) are ignored.
    """
    merged: dict[str, dict] = {}
    for s in shares:
        kind, target = s.get("share_type"), str(s.get("share_with") or "")
        if kind == GROUP_SHARE and not target.startswith(group_prefix):
            log.info("share %s ignored: group has no %r prefix", s.get("id"), group_prefix)
            continue
        if kind not in (USER_SHARE, GROUP_SHARE) or not s.get("path") or not s.get("uid_owner"):
            continue
        m = merged.setdefault(normalise(s["path"]), {"owners": {}, "groups": set(), "create": False})
        m["owners"].setdefault(s["uid_owner"], s.get("displayname_owner") or s["uid_owner"])
        m["create"] |= bool(int(s.get("permissions") or 0) & CREATE)
        if kind == GROUP_SHARE:
            m["groups"].add(target[len(group_prefix) :])
    return [
        Mount(
            path,
            tuple(sorted(m["owners"])),
            ", ".join(sorted(m["owners"].values())),
            tuple(sorted(m["groups"])),
            can_create=m["create"],
            # Shared with a group, or by several people, it would be used on others' behalf: not personal.
            personal=_personal(path, m),
        )
        for path, m in sorted(merged.items())
    ]


def _personal(path: str, merged: dict) -> bool:
    return (
        bool(PERSONAL.fullmatch(PurePosixPath(path).name))
        and not merged["groups"]
        and len(merged["owners"]) == 1
    )


class Shares:
    """Nextcloud's sharing API as the uriel account."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            transport=transport,
            timeout=timeout,
            headers={"OCS-APIRequest": "true"},
        )

    async def _ocs(self, method: str, path: str, **params: str) -> list | dict:
        try:
            r = await self._client.request(method, f"{OCS}{path}", params={**params, "format": "json"})
        except httpx.TransportError as e:
            raise Unreachable(type(e).__name__) from e
        if r.status_code == 401:
            raise AuthFailed("OCS 401")
        if r.status_code >= 500:
            raise Unreachable(f"OCS {r.status_code}")
        if r.status_code >= 400:
            raise WebDavError(f"OCS {r.status_code}")
        return r.json()["ocs"]["data"]

    async def shared_with_me(self) -> list[dict]:
        """Accept anything pending first: sharing with uriel is itself the owner's consent. With Nextcloud's
        default auto-accept there is never anything pending."""
        for pending in await self._ocs("GET", "/shares/pending"):
            await self._ocs("POST", f"/shares/pending/{pending['id']}")
        return list(await self._ocs("GET", "/shares", shared_with_me="true"))

    async def aclose(self) -> None:
        await self._client.aclose()
