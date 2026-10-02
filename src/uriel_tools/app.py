import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.middleware import Middleware as ASGIMiddleware

from uriel_tools.gating import ToolPolicy
from uriel_tools.issues import Forgejo
from uriel_tools.memory.questionnaire import Questionnaire
from uriel_tools.middleware import ApiKeyMiddleware, GroupGate
from uriel_tools.schedules.caldav import CalDav
from uriel_tools.schedules.rules import DEFAULT_TZ
from uriel_tools.search import WebSearch
from uriel_tools.tools import Rag, register_tools

# A long-running job started with the server, e.g. the calendar mirror; it returns once `stop` is set.
Background = Callable[[asyncio.Event], Awaitable[None]]


def _running(jobs: Sequence[Background]):
    @asynccontextmanager
    async def lifespan(server: FastMCP):
        stop = asyncio.Event()
        tasks = [asyncio.create_task(job(stop)) for job in jobs]
        try:
            yield {}
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    return lifespan


def build_mcp(
    policy: ToolPolicy,
    rag: Rag | None = None,
    issues: Forgejo | None = None,
    questionnaire: Questionnaire | None = None,
    *,
    web: WebSearch | None = None,
    default_tz: str = DEFAULT_TZ,
    background: Sequence[Background] = (),
    calendars: CalDav | None = None,
) -> FastMCP:
    lifespan = {"lifespan": _running(background)} if background else {}
    mcp = FastMCP("uriel-tools", middleware=[GroupGate(policy)], **lifespan)
    register_tools(
        mcp, rag, issues, questionnaire, policy=policy, web=web, default_tz=default_tz, calendars=calendars
    )
    return mcp


def build_app(
    policy: ToolPolicy,
    api_keys: list[str],
    rag: Rag | None = None,
    issues: Forgejo | None = None,
    questionnaire: Questionnaire | None = None,
    *,
    web: WebSearch | None = None,
    default_tz: str = DEFAULT_TZ,
    background: Sequence[Background] = (),
    calendars: CalDav | None = None,
):
    mcp = build_mcp(
        policy,
        rag,
        issues,
        questionnaire,
        web=web,
        default_tz=default_tz,
        background=background,
        calendars=calendars,
    )
    return mcp.http_app(middleware=[ASGIMiddleware(ApiKeyMiddleware, keys=api_keys)])
