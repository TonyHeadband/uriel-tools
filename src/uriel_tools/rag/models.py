from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Role = Literal["embedding", "ocr", "rerank"]


class ModelSpec(BaseModel):
    """An OpenAI-compatible endpoint; base_url ends in /v1."""

    base_url: str
    model: str
    api_key: str = "unused"
    timeout_s: float = 60.0
    params: dict[str, Any] = Field(default_factory=dict)


class ModelsConfig(BaseModel):
    """One models.yaml is shared by the whole deployment (it lives in uriel-agent's config/). This process
    needs only its own roles; roles and fields for other processes (chat, deciders, provider) are ignored."""

    models: dict[str, ModelSpec]
    roles: dict[str, str]

    @model_validator(mode="after")
    def _check_roles(self) -> "ModelsConfig":
        missing = [r for r in ("embedding", "ocr") if r not in self.roles]
        if missing:
            raise ValueError(f"missing role(s): {', '.join(missing)}")
        unknown = sorted(
            {self.roles[r] for r in ("embedding", "ocr", "rerank") if r in self.roles} - set(self.models)
        )
        if unknown:
            raise ValueError(f"unknown model reference(s): {', '.join(unknown)}")
        return self

    def has(self, role: Role) -> bool:
        return role in self.roles

    def role(self, role: Role) -> ModelSpec:
        return self.models[self.roles[role]]
