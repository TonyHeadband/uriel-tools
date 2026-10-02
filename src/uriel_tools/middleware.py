import hmac
import json
import logging
import time

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.responses import JSONResponse

from uriel_tools.gating import ToolPolicy, caller_from

audit = logging.getLogger("uriel.mcp.audit")


class ApiKeyMiddleware:
    """Pure ASGI rather than BaseHTTPMiddleware, which can break SSE streaming."""

    def __init__(self, app, keys: list[str]):
        if not keys:
            raise ValueError("at least one MCP API key is required")
        self.app = app
        self.keys = [k.encode() for k in keys]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"x-api-key", b"")
            ok = False
            for key in self.keys:  # check every key so timing doesn't reveal which one matched
                ok |= hmac.compare_digest(got, key)
            if not ok:
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class GroupGate(Middleware):
    def __init__(self, policy: ToolPolicy):
        self.policy = policy

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        caller = caller_from(context.fastmcp_context)
        return [
            t.model_copy(update={"meta": {**(t.meta or {}), "uriel": self._uriel_meta(t.name)}})
            for t in tools
            if self.policy.allowed(t.name, caller.groups)
        ]

    def _uriel_meta(self, name: str) -> dict:
        rule = self.policy.tools[name]
        return {
            "category": rule.category,
            "hidden": rule.hidden,
            "unattended": rule.unattended,
            "companion_action": rule.companion_action,
        }

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        caller = caller_from(context.fastmcp_context)
        name = context.message.name
        allowed = self.policy.allowed(name, caller.groups)
        start = time.perf_counter()
        try:
            if not allowed:
                raise ToolError(f"{name} is not permitted for {caller.user or 'an unidentified caller'}")
            return await call_next(context)
        finally:
            audit.info(
                json.dumps(
                    {
                        "request_id": caller.request_id,
                        "user": caller.user,
                        "tool": name,
                        "allowed": allowed,
                        "duration_ms": round((time.perf_counter() - start) * 1000),
                    }
                )
            )
