"""OpenAI-compatible stand-in for Ollama in the e2e stack: hash embeddings and a canned OCR answer."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.fakes.models import hash_vector

OCR_TEXT = "RICEVUTA 7781 pagata"


async def embeddings(request: Request) -> JSONResponse:
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    data = [{"object": "embedding", "index": i, "embedding": hash_vector(t)} for i, t in enumerate(inputs)]
    return JSONResponse({"object": "list", "model": body["model"], "data": data})


async def chat(request: Request) -> JSONResponse:
    message = {"role": "assistant", "content": OCR_TEXT}
    return JSONResponse({"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]})


async def unload(request: Request) -> JSONResponse:
    return JSONResponse({"done": True})


app = Starlette(
    routes=[
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/api/generate", unload, methods=["POST"]),
    ]
)
