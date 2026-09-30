import math
from pathlib import PurePosixPath

import httpx

from uriel_tools.rag.models import ModelSpec

DIMENSIONS = 1024
QUERY_INSTRUCTION = "Instruct: Given a household question, retrieve passages that answer it\nQuery: "


class ModelUnavailable(Exception):
    """The model server is down, overloaded or timing out: the work should wait, not fail."""


def document_input(path: str, folder_path: str, text: str) -> str:
    return f"{PurePosixPath(path).name} — {folder_path}\n{text}"


def model_client(spec: ModelSpec, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=spec.base_url.rstrip("/"),
        timeout=spec.timeout_s,
        headers={"Authorization": f"Bearer {spec.api_key}"},
        transport=transport,
    )


async def post_json(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    try:
        r = await client.post(url, json=body)
    except httpx.TransportError as e:  # includes timeouts
        raise ModelUnavailable(type(e).__name__) from e
    if r.status_code >= 500 or r.status_code in (404, 429):
        # 404 means the model isn't pulled/named as configured (Ollama): a config error, not a bad
        # document, so it should make the pipeline wait rather than fail the corpus after 5 retries.
        raise ModelUnavailable(f"model server answered {r.status_code}")
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(f"model server answered {r.status_code}", request=r.request, response=r)
    return r.json()


class Embedder:
    def __init__(self, spec: ModelSpec, *, transport: httpx.AsyncBaseTransport | None = None):
        self.model = spec.model
        self._params = spec.params
        self._client = model_client(spec, transport)

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        inputs = [QUERY_INSTRUCTION + t for t in texts] if query else texts
        body = {**self._params, "model": self.model, "input": inputs, "dimensions": DIMENSIONS}
        data = (await post_json(self._client, "/embeddings", body))["data"]
        return [_fit(d["embedding"]) for d in sorted(data, key=lambda d: d["index"])]


def _fit(v: list[float]) -> list[float]:
    # Matryoshka embeddings stay meaningful when truncated; re-normalise after cutting.
    if len(v) < DIMENSIONS:
        raise ValueError(f"embedding has {len(v)} dimensions, need {DIMENSIONS}")
    v = v[:DIMENSIONS]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]
