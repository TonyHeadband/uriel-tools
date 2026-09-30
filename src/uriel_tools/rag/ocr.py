import base64
import contextlib
import io

import httpx
import pypdfium2 as pdfium
from PIL import Image, ImageSequence, UnidentifiedImageError

from uriel_tools.rag.embed import model_client, post_json
from uriel_tools.rag.extract import PDF, Skipped
from uriel_tools.rag.models import ModelSpec

OCR_PROMPT = (
    "Transcribe all text in this image exactly as written. Do not correct, translate or summarise. "
    "Keep line breaks and reading order. Output only the transcription."
)
RENDER_DPI = 150
MAX_SIDE = 2000


class OcrModel:
    def __init__(self, spec: ModelSpec, *, transport: httpx.AsyncBaseTransport | None = None):
        self.model = spec.model
        self._params = spec.params
        self._root = spec.base_url.rstrip("/").removesuffix("/v1")
        self._client = model_client(spec, transport)

    async def transcribe(self, png: bytes) -> str:
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        body = {
            **self._params,
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
        }
        reply = await post_json(self._client, "/chat/completions", body)
        return (reply["choices"][0]["message"].get("content") or "").strip()

    async def release(self) -> None:
        """Ask Ollama to unload the vision model so the shared GPU goes back to chat. Best effort."""
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(f"{self._root}/api/generate", json={"model": self.model, "keep_alive": 0})


def render_pages(data: bytes, mime: str, pages: list[int]) -> list[tuple[int, bytes]]:
    if mime == PDF:
        pdf = pdfium.PdfDocument(data)
        try:
            return [(n, _png(pdf[n - 1].render(scale=RENDER_DPI / 72).to_pil())) for n in pages]
        finally:
            pdf.close()
    try:
        with Image.open(io.BytesIO(data)) as img:
            frames = {n: frame.copy() for n, frame in enumerate(ImageSequence.Iterator(img), 1) if n in pages}
    except (UnidentifiedImageError, Image.DecompressionBombError) as e:
        raise Skipped(f"unreadable image: {type(e).__name__}") from e
    return [(n, _png(frames[n])) for n in pages if n in frames]


def _png(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
