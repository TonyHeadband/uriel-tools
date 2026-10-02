from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from fastmcp import Context
from pydantic import BaseModel, Field

# What a call looks like from outside, for the desktop companion's character: lookup reads, change writes now,
# draft prepares a change that waits for a yes, confirm applies one, ask puts a question to the person.
CompanionAction = Literal["lookup", "change", "draft", "confirm", "ask"]


class ToolRule(BaseModel):
    groups: list[str] = Field(min_length=1)
    # Which part of the household a tool serves; the agent narrows the model's tools by it (since 0.7.0).
    category: str = Field(min_length=1)
    hidden: bool = False  # for the gateway itself, never offered to the model (since 0.8.0)
    unattended: bool = False  # safe for a scheduled run, with nobody there to confirm anything (since 0.8.0)
    companion_action: CompanionAction | None = None  # absent only on hidden tools (since 0.10.0)


class ToolPolicy(BaseModel):
    tools: dict[str, ToolRule]

    def allowed(self, tool: str, groups: Iterable[str]) -> bool:
        rule = self.tools.get(tool)
        return rule is not None and not set(rule.groups).isdisjoint(groups)

    def unattended_tools(self) -> frozenset[str]:
        return frozenset(name for name, rule in self.tools.items() if rule.unattended)


@dataclass(frozen=True)
class Caller:
    user: str | None
    groups: frozenset[str]
    request_id: str | None
    sub: str | None = None  # the OIDC subject, optional (contract since 0.3.0)

    @property
    def nextcloud_ids(self) -> list[str]:
        """The Nextcloud uids this caller may own folders under: accounts sociallogin created are named
        `authelia-<sub>`, local ones after the username."""
        ids = [self.user] if self.user else []
        if self.sub:
            ids.append(f"authelia-{self.sub}")
        return ids


def caller_from(ctx: Context | None) -> Caller:
    # Identity arrives as MCP _meta set by the gateway, which the API key authenticates.
    meta = ctx.request_context.meta if ctx and ctx.request_context else None
    extra = meta.model_dump(exclude_none=True) if meta else {}
    return Caller(
        extra.get("user"), frozenset(extra.get("groups", [])), extra.get("request_id"), extra.get("sub")
    )
