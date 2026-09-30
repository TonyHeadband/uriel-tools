import hashlib
import math
import re

from uriel_tools.rag.embed import DIMENSIONS, ModelUnavailable


def hash_vector(text: str) -> list[float]:
    """A deterministic bag-of-words vector: texts sharing words are close, so search tests stay meaningful."""
    v = [0.0] * DIMENSIONS
    for word in re.findall(r"\w+", text.lower()):
        v[int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        v[0], norm = 1.0, 1.0
    return [x / norm for x in v]


class HashEmbedder:
    model = "hash-embed"

    def __init__(self):
        self.fail = False
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        if self.fail:
            raise ModelUnavailable("fake embedder is down")
        self.calls.append(texts)
        return [hash_vector(t) for t in texts]


class FakeOcr:
    model = "fake-ocr"

    def __init__(self, text: str = "testo scansionato"):
        self.text = text
        self.fail = False
        self.calls = 0
        self.released = 0

    async def transcribe(self, png: bytes) -> str:
        if self.fail:
            raise ModelUnavailable("fake OCR is down")
        assert png.startswith(b"\x89PNG")
        self.calls += 1
        return self.text

    async def release(self) -> None:
        self.released += 1
