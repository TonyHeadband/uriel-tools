import httpx
import pytest

from tests.dbutil import folders, meta
from tests.fakes.models import HashEmbedder, hash_vector
from uriel_tools.gating import Caller
from uriel_tools.rag import store
from uriel_tools.rag.search import search

pytestmark = pytest.mark.db


class RaisingEmbedder:
    model = "hash-embed"

    def __init__(self, exc: Exception):
        self._exc = exc

    async def embed(self, texts, *, query: bool = False):
        raise self._exc


def caller(user, *groups, sub=None):
    return Caller(user, frozenset(groups), "r1", sub)


LIL_B = "19e1"  # her Nextcloud account, created by sociallogin, is authelia-19e1


async def add(pool, folder, path, texts, status="indexed", model="hash-embed", fileid=7):
    chunks = [store.NewChunk(i + 1, "text", t, hash_vector(t)) for i, t in enumerate(texts)]
    await store.save_document(pool, folder.id, meta(path, fileid=fileid), status, chunks, model)


@pytest.fixture
async def library(pool):
    mine, shared, other = await folders(
        pool,
        {"owner": "anthony", "path": "/Documents"},
        {"owner": "anthony", "path": "/Famiglia", "share_with": ["family"]},
        {"owner": "lil-b", "nextcloud_user": "authelia-19e1", "path": "/Scuola"},
    )
    await add(pool, mine, "/Documents/polizza.pdf", ["Polizza casa numero AB-1234, rinnovo il primo marzo"])
    await add(pool, shared, "/Famiglia/caldaia.pdf", ["Caldaia: premere reset per cinque secondi"])
    await add(pool, other, "/Scuola/pagella.pdf", ["Pagella: storia otto, caldaia della scuola rotta"])
    return pool


async def titles(pool, who, query, **kw):
    result = await search(pool, HashEmbedder(), who, query, **kw)
    return sorted(h["title"] for h in result["hits"])


async def best(pool, who, query):
    return (await search(pool, HashEmbedder(), who, query))["hits"][0]["title"]


# Vector search ranks every visible chunk, relevant or not, so visibility is checked with a broad query and
# relevance by the top hit.
BROAD = "caldaia polizza pagella"


async def test_access_matrix(library):
    # anthony owns /Famiglia (as well as /Documents), so he sees it with or without the family group.
    assert await titles(library, caller("anthony", "family"), BROAD, k=10) == ["caldaia.pdf", "polizza.pdf"]
    assert await titles(library, caller("anthony"), BROAD, k=10) == ["caldaia.pdf", "polizza.pdf"]
    lil_b = ["caldaia.pdf", "pagella.pdf"]
    assert await titles(library, caller("lil-b", "family", sub=LIL_B), BROAD, k=10) == lil_b
    assert await titles(library, caller("lil-b", sub=LIL_B), BROAD, k=10) == ["pagella.pdf"]
    # A non-owner outside the shared group sees neither the owner's private folder nor the shared one.
    assert await titles(library, caller("guest"), BROAD, k=10) == []


async def test_a_sociallogin_account_is_matched_through_sub_only(library):
    # Without her OIDC subject, "lil-b" is not the owner of authelia-19e1's folder.
    assert await titles(library, caller("lil-b", "family"), BROAD, k=10) == ["caldaia.pdf"]
    assert await titles(library, caller("guest", sub="nope"), BROAD, k=10) == []


async def test_hits_from_someone_elses_folder_say_who_shared_it(library):
    [hit] = (
        await search(library, HashEmbedder(), caller("lil-b", "family", sub=LIL_B), "caldaia reset", k=1)
    )["hits"]
    assert (hit["title"], hit["shared_by"]) == ("caldaia.pdf", "anthony")
    [own] = (await search(library, HashEmbedder(), caller("anthony", "family"), "caldaia reset", k=1))["hits"]
    assert "shared_by" not in own


async def test_best_match_ranks_first(library):
    assert await best(library, caller("anthony", "family"), "caldaia reset") == "caldaia.pdf"
    assert await best(library, caller("anthony", "family"), "polizza rinnovo") == "polizza.pdf"


async def test_no_identity_sees_nothing(library):
    assert (await search(library, HashEmbedder(), Caller(None, frozenset({"family"}), None), "caldaia"))[
        "hits"
    ] == []


async def test_folder_filter_cannot_widen_access(library):
    assert await titles(library, caller("lil-b", "family"), BROAD, folder="/Documents") == []
    assert await titles(library, caller("anthony", "family"), BROAD, folder="Famiglia/") == ["caldaia.pdf"]


async def test_exact_tokens_are_found_by_full_text(library):
    assert await best(library, caller("anthony", "family"), "AB-1234") == "polizza.pdf"


async def test_degraded_when_embeddings_are_down(library):
    embedder = HashEmbedder()
    embedder.fail = True
    result = await search(library, embedder, caller("anthony"), "polizza")
    assert result["degraded"] == "semantic search unavailable"
    assert [h["title"] for h in result["hits"]] == ["polizza.pdf"]


_404 = httpx.HTTPStatusError("boom", request=httpx.Request("POST", "http://x"), response=httpx.Response(404))


@pytest.mark.parametrize("exc", [_404, ValueError("embedding has 768 dimensions, need 1024")])
async def test_degraded_on_any_query_embedding_failure(library, exc):
    result = await search(library, RaisingEmbedder(exc), caller("anthony"), "polizza")
    assert result["degraded"] == "semantic search unavailable"
    assert [h["title"] for h in result["hits"]] == ["polizza.pdf"]


async def test_hit_shape_and_links(library):
    # anthony also owns /Famiglia, so scope to /Documents: this test is about hit shape, not access.
    [hit] = (
        await search(
            library,
            HashEmbedder(),
            caller("anthony"),
            "polizza",
            folder="/Documents",
            public_url="https://cloud.example/",
        )
    )["hits"]
    assert hit == {
        "title": "polizza.pdf",
        "folder": "/Documents",
        "path": "/Documents/polizza.pdf",
        "page": 1,
        "source": "text",
        "snippet": "Polizza casa numero AB-1234, rinnovo il primo marzo",
        "modified": "2026-03-02",
        "url": "https://cloud.example/f/7",
    }
    [hit] = (await search(library, HashEmbedder(), caller("anthony"), "polizza", folder="/Documents"))["hits"]
    assert hit["url"] is None


async def test_per_document_cap_k_cap_and_snippet_length(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await add(pool, f, "/D/manuale.pdf", [f"lavatrice programma {i} " + "x" * 700 for i in range(5)])
    for i in range(12):
        await add(pool, f, f"/D/doc{i}.txt", [f"lavatrice nota {i}"])
    result = await search(pool, HashEmbedder(), caller("a"), "lavatrice", k=50)
    hits = result["hits"]
    assert len(hits) == 10
    assert sum(h["title"] == "manuale.pdf" for h in hits) <= 2
    assert all(len(h["snippet"]) <= 600 for h in hits)


async def test_awaiting_ocr_is_counted(library):
    [mine] = [f for f in await store.list_folders(library) if f.mount == "/Documents"]
    await add(library, mine, "/Documents/scan.png", [], status="awaiting_ocr")
    assert (await search(library, HashEmbedder(), caller("anthony"), "polizza"))["awaiting_ocr"] == 1


async def test_stale_model_chunks_are_found_by_text_only(pool):
    [f] = await folders(pool, {"owner": "a", "path": "/D"})
    await add(pool, f, "/D/vecchio.txt", ["bolletta gas marzo"], model="old-model")
    assert await titles(pool, caller("a"), "bolletta") == ["vecchio.txt"]
