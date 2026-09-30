"""Editing on the caller's behalf: never changes a file, always saves a new edit_ draft next to it."""

import asyncio
from collections.abc import Callable
from pathlib import PurePosixPath

from psycopg_pool import AsyncConnectionPool

from uriel_tools.edit import office, pdf, records
from uriel_tools.edit.core import NotEditable, Result
from uriel_tools.edit.names import draft_path
from uriel_tools.gating import Caller
from uriel_tools.rag import store
from uriel_tools.rag.extract import DOCX, ODT, PDF, resolve_mime
from uriel_tools.rag.sources import normalise, within
from uriel_tools.rag.store import Folder
from uriel_tools.rag.webdav import Exists, NotFound, WebDav

MAX_BYTES = 50 * 1024 * 1024
EDIT_TIMEOUT_S = 60
SIGNATURE_FILES = ("firma.png", "signature.png")
TEXT_TYPES = {DOCX, ODT, "text/plain", "text/markdown"}
REVIEW = (
    "Saved as a new file next to the original; the original is unchanged. Review it in Nextcloud before use."
)


class Denied(Exception):
    """Refused, with a message the model can relay as is."""


class Editor:
    def __init__(self, pool: AsyncConnectionPool, dav: Callable[[], WebDav], public_url: str = ""):
        self.pool, self.dav, self.public_url = pool, dav, public_url.rstrip("/")

    # ---- access

    async def _folder(self, caller: Caller, path: str, *, write: bool = False) -> Folder:
        """The rule search uses: folders the caller shared with Uriel, or with a Uriel group of theirs."""
        ids, groups = set(caller.nextcloud_ids), set(caller.groups)
        visible = [
            f
            for f in await store.list_folders(self.pool)
            if within(f.mount, path)
            and not f.personal
            and (ids & set(f.owners) or groups & set(f.share_with))
        ]
        if not caller.user or not visible:
            raise Denied("not found among the documents you can see")
        folder = max(visible, key=lambda f: len(f.mount))
        if folder.state != "ok":
            raise Denied("document access is paused right now; try again later")
        if write and not folder.can_create:
            raise Denied(
                f"{folder.mount} is shared with Uriel read-only, so it can't save an edited copy there. "
                f"Ask {folder.shared_by} to share it again with 'Allow creating' ticked."
            )
        return folder

    async def _signature(self, dav: WebDav, caller: Caller) -> bytes:
        ids = set(caller.nextcloud_ids)
        found = []
        for f in await store.list_folders(self.pool):
            if f.personal and ids & set(f.owners):
                found += [
                    e
                    for e in await dav.children(f.mount)
                    if PurePosixPath(e.path).name.lower() in SIGNATURE_FILES
                ]
        if not found:
            raise Denied(
                "no signature image found: put firma.png (a transparent PNG of your signature) in a folder "
                "named 'Uriel' and share that folder with the user uriel"
            )
        if len(found) > 1:
            raise Denied("more than one signature image: " + ", ".join(e.path for e in found) + "; keep one")
        return await dav.get(found[0].path, 5 * 1024 * 1024)

    # ---- plumbing

    async def _load(self, dav: WebDav, path: str) -> tuple[bytes, str]:
        try:
            entry = await dav.stat(path)
        except NotFound as e:
            raise Denied("not found among the documents you can see") from e
        if entry.is_dir:
            raise Denied("that's a folder; name a file inside it")
        if entry.size > MAX_BYTES:
            raise Denied(f"too large to edit (over {MAX_BYTES // 2**20} MB)")
        return await dav.get(path, MAX_BYTES), resolve_mime(entry.mime, entry.path)

    @staticmethod
    async def _run(fn, *args) -> Result:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), EDIT_TIMEOUT_S)
        except TimeoutError as e:
            raise Denied(f"the edit took longer than {EDIT_TIMEOUT_S} s and was abandoned") from e

    async def _save(
        self, dav: WebDav, folder: Folder, source: str, mime: str, result: Result, tool: str, caller: Caller
    ) -> dict:
        parent = str(PurePosixPath(source).parent)
        taken = {PurePosixPath(e.path).name for e in await dav.children(parent)}
        for _ in range(50):
            target = draft_path(source, taken)
            try:
                await dav.put_new(target, result.data, mime)
                break
            except Exists:  # someone created that name meanwhile: take the next number
                taken.add(PurePosixPath(target).name)
        else:
            raise Denied("couldn't find a free name for the edited copy")
        entry = await dav.stat(target)
        await records.record(
            self.pool,
            folder_id=folder.id,
            source=source,
            output=target,
            fileid=entry.fileid,
            etag=entry.etag,
            tool=tool,
            caller=caller.user or "",
            items=result.items,
        )
        out = {
            "saved": target,
            "url": f"{self.public_url}/f/{entry.fileid}" if self.public_url and entry.fileid else None,
            "done": [_describe(i) for i in result.items],
            "not_found": result.not_found,
            "approximate": result.approximate,
            "warnings": result.warnings,
            "note": REVIEW,
        }
        if result.approximate:
            out["note"] += " Some values were placed by position: check they sit in the right boxes."
        return out

    # ---- tools

    async def inspect(self, caller: Caller, path: str) -> dict:
        path = normalise(path)
        folder = await self._folder(caller, path)
        dav = self.dav()
        try:
            data, mime = await self._load(dav, path)
        finally:
            await dav.aclose()
        if mime == PDF:
            info = await asyncio.to_thread(pdf.inspect, data)
        elif mime in TEXT_TYPES:
            info = await asyncio.to_thread(office.inspect, data, mime)
        else:
            raise Denied(f"Uriel can't edit this type of file ({mime or 'unknown'})")
        info["can_save_edits"] = folder.can_create
        if not folder.can_create:
            info["why_not"] = f"shared read-only; ask {folder.shared_by} to allow creating files"
        return info

    async def fill(self, caller: Caller, path: str, values: dict[str, str]) -> dict:
        return await self._edit(caller, path, "fill_form", lambda data, mime: _fill(data, mime, values))

    async def edit_text(
        self, caller: Caller, path: str, changes: list[tuple[str, str]], notes: list[tuple[str, str]]
    ) -> dict:
        return await self._edit(
            caller, path, "edit_text", lambda data, mime: _text(data, mime, changes, notes)
        )

    async def sign(self, caller: Caller, path: str, where: str | None, confirmed: bool) -> dict:
        path = normalise(path)
        folder = await self._folder(caller, path, write=True)
        dav = self.dav()
        try:
            data, mime = await self._load(dav, path)
            if mime != PDF:
                raise Denied("signatures can be placed on PDFs only")
            image = await self._signature(dav, caller)
            if not confirmed:
                spot = await asyncio.to_thread(pdf.describe_signature, data, where)
                return {
                    "confirmed": False,
                    "would_place": spot,
                    "next": "Ask the person to confirm in the chat; only after they say yes, call again with "
                    "confirmed=true. Nothing has been saved.",
                }
            result = await self._run(pdf.sign, data, image, where)
            return await self._save(dav, folder, path, mime, result, "sign_document", caller)
        except NotEditable as e:
            raise Denied(str(e)) from e
        finally:
            await dav.aclose()

    async def confirm(self, caller: Caller, path: str) -> dict:
        path = normalise(path)
        folder = await self._folder(caller, path)
        if not await records.confirm(self.pool, folder.id, path):
            raise Denied("that isn't an edited copy Uriel made")
        return {"confirmed": path}

    async def _edit(self, caller: Caller, path: str, tool: str, apply) -> dict:
        path = normalise(path)
        folder = await self._folder(caller, path, write=True)
        dav = self.dav()
        try:
            data, mime = await self._load(dav, path)
            result = await self._run(apply, data, mime)
            if not result.items:
                return {
                    "saved": None,
                    "not_found": result.not_found,
                    "warnings": result.warnings,
                    "note": "Nothing matched, so no copy was saved.",
                }
            return await self._save(dav, folder, path, mime, result, tool, caller)
        except NotEditable as e:
            raise Denied(str(e)) from e
        finally:
            await dav.aclose()


def _fill(data: bytes, mime: str, values: dict[str, str]) -> Result:
    if mime != PDF:
        raise NotEditable("fill_form works on PDFs; use edit_text to change the text of other documents")
    return pdf.fill(data, values)


def _text(data: bytes, mime: str, changes: list[tuple[str, str]], notes: list[tuple[str, str]]) -> Result:
    if mime == PDF:
        noted = pdf.add_notes(data, notes) if notes else Result(data)
        if not changes:
            return noted
        replaced = pdf.replace_text(noted.data, changes)
        return Result(
            replaced.data,
            noted.items + replaced.items,
            noted.not_found + replaced.not_found,
            noted.warnings + replaced.warnings,
        )
    if mime in TEXT_TYPES:
        return office.edit(data, mime, changes, notes)
    raise NotEditable(f"Uriel can't edit this type of file ({mime or 'unknown'})")


def _describe(item) -> str:
    where = f" (page {item.page})" if item.page else ""
    return f"{item.item} → {item.value}{where}"
