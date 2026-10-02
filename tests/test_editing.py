from datetime import UTC, datetime

import pymupdf
import pytest

from tests import filegen
from tests.dbutil import doc
from tests.fakes.models import FakeOcr, HashEmbedder
from tests.fakes.webdav import FakeNextcloud
from uriel_tools.edit.service import Denied, Editor
from uriel_tools.gating import Caller
from uriel_tools.indexer.pipeline import Indexer
from uriel_tools.indexer.reconcile import reconcile
from uriel_tools.rag import store
from uriel_tools.rag.jobs import Job
from uriel_tools.rag.search import search
from uriel_tools.rag.shares import Mount, mounts_from
from uriel_tools.rag.sources import SourcesConfig
from uriel_tools.rag.webdav import WebDav

pytestmark = pytest.mark.db

ANTHONY = Caller("anthony-headband", frozenset({"family"}), "r1")
LIL_B = Caller("lil-b", frozenset({"family"}), "r2", sub="19e1")
GUEST = Caller("guest", frozenset(), "r3")
PDF_MIME = "application/pdf"


@pytest.fixture
async def env(pool):
    nc = FakeNextcloud({"uriel": "pw"})
    nc.put("uriel", "/Documents/Scuola/iscrizione.pdf", filegen.form_pdf(), PDF_MIME)
    nc.put("uriel", "/Documents/Scuola/modulo.pdf", filegen.flat_form_pdf(), PDF_MIME)
    nc.put("uriel", "/Casa/contratto.pdf", filegen.form_pdf(), PDF_MIME)
    nc.put("uriel", "/Famiglia/lettera.docx", filegen.styled_docx(), "application/octet-stream")
    nc.put("uriel", "/Famiglia/delega.pdf", filegen.flat_form_pdf(), PDF_MIME)
    nc.put("uriel", "/Uriel/firma.png", filegen.signature_png(), "image/png")
    await store.sync_mounts(
        pool,
        [
            Mount("/Documents", ("anthony-headband",), "anthony-headband", (), can_create=True),
            Mount("/Casa", ("anthony-headband",), "anthony-headband", ()),  # read-only share
            Mount("/Famiglia", ("authelia-19e1",), "Lil-B", ("family",), can_create=True),
            Mount("/Uriel", ("anthony-headband",), "anthony-headband", (), personal=True),
        ],
    )
    editor = Editor(
        pool, lambda: WebDav("http://nc", "uriel", "pw", transport=nc.transport()), "https://cloud.x"
    )
    return editor, nc


async def edits(pool):
    async with pool.connection() as conn:
        return await (await conn.execute("SELECT * FROM edits ORDER BY id")).fetchall()


async def test_fill_saves_a_new_draft_and_records_it(pool, env):
    editor, nc = env
    original = nc.files[("uriel", "/Documents/Scuola/iscrizione.pdf")].data
    out = await editor.fill(ANTHONY, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Rossi"})
    assert out["saved"] == "/Documents/Scuola/edit_iscrizione.pdf"
    assert out["url"].startswith("https://cloud.x/f/") and out["done"] == ["cognome → Rossi (page 1)"]
    assert nc.files[("uriel", "/Documents/Scuola/iscrizione.pdf")].data == original  # never changed
    saved = pymupdf.open(stream=nc.files[("uriel", out["saved"])].data, filetype="pdf")
    assert {w.field_name: w.field_value for p in saved for w in p.widgets()}["cognome"] == "Rossi"
    [row] = await edits(pool)
    assert (row["status"], row["tool"], row["caller"]) == ("draft", "fill_form", "anthony-headband")
    again = await editor.fill(ANTHONY, out["saved"], {"cognome": "Bianchi"})  # continue from the draft
    assert again["saved"] == "/Documents/Scuola/edit_iscrizione (2).pdf"


async def test_flat_form_placement_is_flagged_approximate(env):
    editor, _ = env
    out = await editor.fill(ANTHONY, "/Documents/Scuola/modulo.pdf", {"Family name": "Rossi", "Nope": "x"})
    assert out["approximate"] and out["not_found"] == ["Nope"] and "check they sit" in out["note"]


async def test_read_only_invisible_and_personal_folders_are_refused(env):
    editor, _ = env
    with pytest.raises(Denied, match="read-only.*Allow creating"):
        await editor.fill(ANTHONY, "/Casa/contratto.pdf", {"cognome": "Rossi"})
    assert (await editor.inspect(ANTHONY, "/Casa/contratto.pdf"))["can_save_edits"] is False
    with pytest.raises(Denied, match="not found among the documents you can see"):
        await editor.fill(GUEST, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Rossi"})
    with pytest.raises(Denied, match="not found"):
        await editor.inspect(ANTHONY, "/Uriel/firma.png")


async def test_a_family_share_can_be_edited_by_the_family(env):
    editor, nc = env
    out = await editor.edit_text(ANTHONY, "/Famiglia/lettera.docx", [("Milano", "Torino")], [])
    assert out["saved"] == "/Famiglia/edit_lettera.docx"
    with pytest.raises(Denied):
        await editor.edit_text(GUEST, "/Famiglia/lettera.docx", [("Milano", "Torino")], [])


async def test_nothing_matched_saves_nothing(env):
    editor, nc = env
    before = set(nc.files)
    out = await editor.edit_text(ANTHONY, "/Famiglia/lettera.docx", [("Roma", "Torino")], [])
    assert out["saved"] is None and out["not_found"] == ["Roma"] and set(nc.files) == before


async def test_signing_places_only_a_previewed_signature_and_only_the_callers_own(pool, env):
    editor, nc = env
    before = set(nc.files)
    preview = await editor.preview_signature(ANTHONY, "/Documents/Scuola/modulo.pdf", None)
    assert "beside 'Signature' on page 2" in preview["would_place"]
    assert set(nc.files) == before  # nothing saved before the person says yes
    done = await editor.place_signature(ANTHONY, preview["draft_id"])
    page = pymupdf.open(stream=nc.files[("uriel", done["saved"])].data, filetype="pdf")[1]
    assert len(page.get_images()) == 1
    # A repeated yes returns the same copy instead of signing twice.
    assert await editor.place_signature(ANTHONY, preview["draft_id"]) == done
    with pytest.raises(Denied, match="no such preview"):
        await editor.place_signature(ANTHONY, "never-previewed")
    # Lil-B may edit the family folder, but has no Uriel folder of her own: anthony's signature is never used,
    # and she hears so before any yes.
    with pytest.raises(Denied, match="no signature image"):
        await editor.preview_signature(LIL_B, "/Famiglia/delega.pdf", None)
    with pytest.raises(Denied, match="no such preview"):
        await editor.place_signature(LIL_B, preview["draft_id"])


async def test_confirm_edit_marks_only_drafts(pool, env):
    editor, _ = env
    out = await editor.fill(ANTHONY, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Rossi"})
    assert await editor.confirm(ANTHONY, out["saved"]) == {"confirmed": out["saved"]}
    assert (await edits(pool))[0]["status"] == "confirmed"
    with pytest.raises(Denied, match="isn't an edited copy"):
        await editor.confirm(ANTHONY, "/Documents/Scuola/iscrizione.pdf")


async def test_a_draft_a_person_edited_becomes_changed_but_confirmed_is_final(pool, env):
    editor, nc = env
    first = await editor.fill(ANTHONY, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Rossi"})
    second = await editor.fill(ANTHONY, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Verdi"})
    await editor.confirm(ANTHONY, second["saved"])
    for path in (first["saved"], second["saved"]):  # someone fixes both in ONLYOFFICE
        nc.put("uriel", path, b"%PDF-1.4 edited", PDF_MIME)
    folder = next(f for f in await store.list_folders(pool) if f.mount == "/Documents")
    dav = WebDav("http://nc", "uriel", "pw", transport=nc.transport())
    await reconcile(pool, dav, folder)
    assert [r["status"] for r in await edits(pool)] == ["changed", "confirmed"]


async def test_personal_folders_are_never_indexed(pool, env):
    _, nc = env
    personal = next(f for f in await store.list_folders(pool) if f.personal)
    dav = WebDav("http://nc", "uriel", "pw", transport=nc.transport())
    assert await reconcile(pool, dav, personal) == (0, 0)
    await doc(pool, personal, "/Uriel/firma.png", ["Mario Rossi"])  # indexed before it became personal
    assert await reconcile(pool, dav, personal) == (0, 1)


async def test_drafts_are_indexed_overnight_only(pool, env):
    _, nc = env
    folder = next(f for f in await store.list_folders(pool) if f.mount == "/Documents")
    nc.put("uriel", "/Documents/edit_nota.txt", b"bozza", "text/plain")
    noon = datetime(2026, 3, 2, 11, 0, tzinfo=UTC)
    indexer = Indexer(
        pool,
        SourcesConfig(),
        lambda f: WebDav("http://nc", "uriel", "pw", transport=nc.transport()),
        HashEmbedder(),
        FakeOcr(),
        now=lambda: noon,
    )
    await indexer.run(Job(0, "index", folder.id, "/Documents/edit_nota.txt", False, 0), folder)
    assert await store.get_document(pool, folder.id, "/Documents/edit_nota.txt") is None
    async with pool.connection() as conn:
        [job] = await (await conn.execute("SELECT kind, run_after FROM jobs")).fetchall()
    assert job["kind"] == "index" and job["run_after"] == datetime(2026, 3, 3, 0, 0, tzinfo=UTC)  # 01:00 Rome


async def test_search_marks_hits_from_drafts(pool, env):
    editor, _ = env
    out = await editor.fill(ANTHONY, "/Documents/Scuola/iscrizione.pdf", {"cognome": "Rossi"})
    folder = next(f for f in await store.list_folders(pool) if f.mount == "/Documents")
    await doc(pool, folder, out["saved"], ["domanda di iscrizione Rossi"], model="hash-embed")
    [hit] = (await search(pool, HashEmbedder(), ANTHONY, "iscrizione Rossi"))["hits"]
    assert (hit["edit_of"], hit["edit_status"]) == ("/Documents/Scuola/iscrizione.pdf", "draft")


def test_share_permissions_and_personal_folders():
    def share(path, owner="a", kind=0, to="uriel", perms=1):
        return {"share_type": kind, "share_with": to, "uid_owner": owner, "path": path, "permissions": perms}

    mounts = {
        m.path: m
        for m in mounts_from(
            [
                share("/Documents", perms=31),
                share("/uriel"),
                share("/Uriel (2)", owner="b"),
                share("/Uriel (3)", owner="c", kind=1, to="uriel-family"),  # group-shared: not personal
                share("/Uriel (4)", owner="d"),
                share("/Uriel (4)", owner="e"),  # two owners: not personal
            ],
            "uriel-",
        )
    }
    assert mounts["/Documents"].can_create and not mounts["/uriel"].can_create
    assert [p for p, m in sorted(mounts.items()) if m.personal] == ["/Uriel (2)", "/uriel"]
