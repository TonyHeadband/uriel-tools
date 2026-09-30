from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator


def normalise(path: str) -> str:
    return "/" + path.strip().strip("/")


def within(parent: str, path: str) -> bool:
    parent, path = normalise(parent), normalise(path)
    return parent == "/" or path == parent or path.startswith(parent + "/")


def next_daily(now: datetime, at: time, tz: str) -> datetime:
    """The next wall-clock `at` in `tz` strictly after `now`, returned in UTC."""
    local = now.astimezone(ZoneInfo(tz))
    candidate = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)  # zoneinfo arithmetic keeps the wall-clock time across DST changes
    return candidate.astimezone(UTC)


class OcrWindow(BaseModel):
    start: time = time(1, 0)
    end: time = time(6, 0)
    tz: str = "Europe/Rome"

    def contains(self, now: datetime) -> bool:
        t = now.astimezone(ZoneInfo(self.tz)).time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_start(self, now: datetime) -> datetime:
        return now if self.contains(now) else next_daily(now, self.start, self.tz)


class SourcesConfig(BaseModel):
    ocr_window: OcrWindow = Field(default_factory=OcrWindow)
    reconcile_at: time = time(0, 30)
    share_poll_minutes: int = Field(default=5, ge=1)
    # A group share counts only when its name has this prefix: `uriel-family` makes a folder searchable by the
    # Authelia group `family`. The prefix keeps indexing a deliberate act (see docs/guide.md).
    share_group_prefix: str = Field(default="uriel-", min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _no_folder_list(cls, data):
        if isinstance(data, dict) and "folders" in data:
            raise ValueError(
                "`folders:` was removed in 0.3.0: folders are opted in by sharing them with uriel"
            )
        return data
