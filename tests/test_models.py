import json
import math

import httpx
import pytest

from tests import filegen
from uriel_tools.rag.embed import DIMENSIONS, Embedder, ModelUnavailable, document_input
from uriel_tools.rag.models import ModelSpec
from uriel_tools.rag.ocr import OcrModel, render_pages

SPEC = ModelSpec(base_url="http://ollama:11434/v1", model="m", params={"reasoning_effort": "none"})


def recorder(response):
    seen = []

    def handler(request):
        seen.append(request)
        return response(request) if callable(response) else response

    return seen, httpx.MockTransport(handler)


def embeddings(n, dims):
    return httpx.Response(
        200, json={"data": [{"index": i, "embedding": [1.0] * dims} for i in reversed(range(n))]}
    )


async def test_embed_sends_dimensions_and_query_instruction():
    seen, transport = recorder(embeddings(1, DIMENSIONS))
    [v] = await Embedder(SPEC, transport=transport).embed(["caldaia"], query=True)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/embeddings"
    assert body["dimensions"] == DIMENSIONS and body["model"] == "m"
    assert body["input"][0].startswith("Instruct: ") and body["input"][0].endswith("Query: caldaia")
    assert len(v) == DIMENSIONS and math.isclose(sum(x * x for x in v), 1.0)


async def test_embed_truncates_normalises_and_keeps_order():
    seen, transport = recorder(embeddings(2, 1536))
    out = await Embedder(SPEC, transport=transport).embed(["a", "b"])
    assert json.loads(seen[0].content)["input"] == ["a", "b"]
    assert [len(v) for v in out] == [DIMENSIONS, DIMENSIONS]


async def test_embed_rejects_short_vectors():
    _, transport = recorder(embeddings(1, 768))
    with pytest.raises(ValueError, match="768"):
        await Embedder(SPEC, transport=transport).embed(["a"])


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(429), httpx.Response(404)])
async def test_embed_server_trouble_is_unavailable(response):
    _, transport = recorder(response)
    with pytest.raises(ModelUnavailable):
        await Embedder(SPEC, transport=transport).embed(["a"])


async def test_embed_connection_error_is_unavailable_and_bad_request_is_not():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelUnavailable):
        await Embedder(SPEC, transport=httpx.MockTransport(down)).embed(["a"])
    _, transport = recorder(httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await Embedder(SPEC, transport=transport).embed(["a"])
    assert "http" not in str(exc_info.value).lower()


def test_document_input_carries_title_and_folder():
    assert (
        document_input("/Documents/Casa/caldaia.pdf", "/Documents", "Reset")
        == "caldaia.pdf — /Documents\nReset"
    )


async def test_ocr_sends_a_literal_prompt_and_the_image():
    seen, transport = recorder(
        httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": " Bolletta 4471 \n"}}]}
        )
    )
    text = await OcrModel(SPEC, transport=transport).transcribe(b"\x89PNG fake")
    body = json.loads(seen[0].content)
    assert text == "Bolletta 4471"
    assert seen[0].url.path == "/v1/chat/completions"
    assert body["temperature"] == 0 and body["reasoning_effort"] == "none"
    parts = body["messages"][0]["content"]
    assert "exactly" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_ocr_release_unloads_and_never_raises():
    seen, transport = recorder(httpx.Response(200, json={}))
    await OcrModel(SPEC, transport=transport).release()
    assert str(seen[0].url) == "http://ollama:11434/api/generate"
    assert json.loads(seen[0].content) == {"model": "m", "keep_alive": 0}

    def down(request):
        raise httpx.ConnectError("refused")

    await OcrModel(SPEC, transport=httpx.MockTransport(down)).release()


async def test_embed_fixed_params_override_config():
    spec_with_bad_dims = ModelSpec(
        base_url="http://ollama:11434/v1", model="m", params={"dimensions": 256, "reasoning_effort": "none"}
    )
    seen, transport = recorder(embeddings(1, DIMENSIONS))
    await Embedder(spec_with_bad_dims, transport=transport).embed(["a"])
    body = json.loads(seen[0].content)
    assert body["dimensions"] == DIMENSIONS


async def test_ocr_fixed_params_override_config():
    spec_with_bad_temp = ModelSpec(
        base_url="http://ollama:11434/v1", model="m", params={"temperature": 0.7, "reasoning_effort": "none"}
    )
    seen, transport = recorder(
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "text"}}]})
    )
    await OcrModel(spec_with_bad_temp, transport=transport).transcribe(b"\x89PNG fake")
    body = json.loads(seen[0].content)
    assert body["temperature"] == 0


def test_render_pdf_and_image_pages():
    [(n, png)] = render_pages(filegen.scanned_pdf(["uno", "due"]), "application/pdf", [2])
    assert n == 2 and png.startswith(b"\x89PNG")
    [(n, png)] = render_pages(filegen.text_png("scontrino"), "image/png", [1])
    assert n == 1 and png.startswith(b"\x89PNG")
