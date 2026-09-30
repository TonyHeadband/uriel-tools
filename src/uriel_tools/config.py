from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


def load_yaml[T: BaseModel](cls: type[T], path: Path | str) -> T:
    return cls.model_validate(yaml.safe_load(Path(path).read_text()))


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URIEL_MCP_")

    api_keys: str  # comma-separated; several keys allow rotation without downtime
    tools_file: Path = Path("config/tools.yaml")
    questionnaire_file: Path = Path("config/questionnaire.yaml")
    host: str = "0.0.0.0"
    # Not `port`: a Kubernetes Service named `uriel-mcp` makes the kubelet inject
    # URIEL_MCP_PORT=tcp://10.43.x.x:8001, which would collide with that field name.
    listen_port: int = 8001

    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]


class RagSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URIEL_")

    tools_database_url: str | None = None  # unset: the MCP server runs without document search
    models_file: Path = Path("config/models.yaml")
    sources_file: Path = Path("config/sources.yaml")
    nc_url: str = ""  # WebDAV base the indexer calls
    nc_user: str = "uriel"  # the service account folders are shared with
    nc_app_password: str = ""
    nc_public_url: str = ""  # base for the /f/<fileid> links returned to the user
    # Not `events_port`: a Service named `uriel-events` makes the kubelet inject URIEL_EVENTS_PORT=tcp://...
    events_listen_port: int = 8002
    events_settle_s: float = 60.0  # delay before an event's reconcile, so a burst of saves folds into one


class ScheduleSettings(BaseSettings):
    """Schedules, their Nextcloud calendar mirror, and web search."""

    model_config = SettingsConfigDict(env_prefix="URIEL_")

    default_tz: str = "America/Toronto"  # for people who haven't set their own
    searxng_url: str = ""  # unset: web_search says it isn't configured
    mirror_enabled: bool | None = None  # unset: on whenever Nextcloud credentials are configured
    mirror_push_seconds: float = 30.0
    mirror_pull_seconds: float = 60.0

    def mirror_on(self, nextcloud_configured: bool) -> bool:
        return nextcloud_configured and self.mirror_enabled is not False


class IssueSettings(BaseSettings):
    """Where file_issue files issues; any field left empty disables the reporting tools."""

    model_config = SettingsConfigDict(env_prefix="URIEL_FORGEJO_")

    url: str = ""  # e.g. https://git.example.com
    token: str = ""  # the uriel bot account's token, scope write:issue
    repo: str = ""  # owner/name, e.g. anthony-headband/uriel-agent

    def configured(self) -> bool:
        return bool(self.url and self.token and self.repo)
