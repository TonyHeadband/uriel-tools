import os

import pytest

from tests import filegen
from uriel_tools.rag.embed import Embedder
from uriel_tools.rag.models import ModelSpec
from uriel_tools.rag.ocr import OcrModel, render_pages

pytestmark = pytest.mark.live
URL = os.environ.get("URIEL_LIVE_OLLAMA_URL", "")  # e.g. http://192.168.1.10:31434/v1
LETTERS = [
    "Comune di Milano: la tassa rifiuti 2026 scade il 16 aprile, importo 312 euro",
    "Mairie de Lyon: la cantine scolaire ferme le 3 mai pour travaux",
]


@pytest.fixture
def models():
    if not URL:
        pytest.skip("URIEL_LIVE_OLLAMA_URL not set")
    embedder = Embedder(ModelSpec(base_url=URL, model="qwen3-embedding:0.6b"))
    ocr = OcrModel(
        ModelSpec(base_url=URL, model="qwen3-vl:8b", timeout_s=600, params={"reasoning_effort": "none"})
    )
    return embedder, ocr


async def test_ocr_then_cross_language_retrieval(models):
    embedder, ocr = models
    texts = []
    try:
        for letter in LETTERS:
            [(_, png)] = render_pages(filegen.scanned_pdf([letter]), "application/pdf", [1])
            texts.append(await ocr.transcribe(png))
    finally:
        await ocr.release()
    assert "312" in texts[0] and "Milano" in texts[0]
    assert "Lyon" in texts[1]
    docs = await embedder.embed([*texts, "Ricetta della torta di mele con cannella"])
    queries = await embedder.embed(
        ["When is the waste tax due?", "When is the school canteen closed?"], query=True
    )

    def best(q):
        return max(range(len(docs)), key=lambda i: sum(a * b for a, b in zip(q, docs[i], strict=True)))

    assert [best(q) for q in queries] == [0, 1]
