import logging
from functools import partial

import uvicorn

from uriel_tools.app import build_app
from uriel_tools.config import IssueSettings, McpSettings, RagSettings, ScheduleSettings, load_yaml
from uriel_tools.db import make_pool
from uriel_tools.edit.service import Editor
from uriel_tools.gating import ToolPolicy
from uriel_tools.issues import Forgejo
from uriel_tools.logs import configure_logging
from uriel_tools.memory.questionnaire import Questionnaire
from uriel_tools.rag.embed import Embedder
from uriel_tools.rag.models import ModelsConfig
from uriel_tools.rag.webdav import WebDav
from uriel_tools.schedules.caldav import CalDav
from uriel_tools.schedules.mirror import Mirror
from uriel_tools.search import WebSearch
from uriel_tools.tools import Rag

log = logging.getLogger("uriel_tools")


def main() -> None:
    configure_logging()
    settings, rag_settings = McpSettings(), RagSettings()
    policy = load_yaml(ToolPolicy, settings.tools_file)
    questionnaire = load_yaml(Questionnaire, settings.questionnaire_file)
    rag = None
    if rag_settings.tools_database_url:
        models = load_yaml(ModelsConfig, rag_settings.models_file)
        if models.has("rerank"):
            log.warning("a rerank role is configured but reranking is not implemented yet; ignoring it")
        pool = make_pool(rag_settings.tools_database_url)
        editor = None
        if rag_settings.nc_url and rag_settings.nc_app_password:
            s = rag_settings
            editor = Editor(pool, lambda: WebDav(s.nc_url, s.nc_user, s.nc_app_password), s.nc_public_url)
        else:
            log.warning("URIEL_NC_URL / URIEL_NC_APP_PASSWORD not set; document editing is disabled")
        rag = Rag(pool, Embedder(models.role("embedding")), rag_settings.nc_public_url, editor)
    else:
        log.warning("URIEL_TOOLS_DATABASE_URL is not set; document search is disabled")
    issue_settings, issues = IssueSettings(), None
    if issue_settings.configured():
        issues = Forgejo(issue_settings.url, issue_settings.token, issue_settings.repo)
    else:
        log.warning("URIEL_FORGEJO_URL / _TOKEN / _REPO not all set; issue reporting is disabled")
    sched, web, background = ScheduleSettings(), None, []
    if sched.searxng_url:
        web = WebSearch(sched.searxng_url)
    else:
        log.warning("URIEL_SEARXNG_URL is not set; web search is disabled")
    nextcloud = bool(rag_settings.nc_url and rag_settings.nc_app_password)
    if rag is not None and sched.mirror_on(nextcloud):
        s = rag_settings
        mirror = Mirror(rag.ready, CalDav(s.nc_url, s.nc_user, s.nc_app_password))
        background.append(
            partial(mirror.run, push_s=sched.mirror_push_seconds, pull_s=sched.mirror_pull_seconds)
        )
    else:
        log.warning("the Nextcloud calendar mirror is off (needs the database and Nextcloud credentials)")
    app = build_app(
        policy,
        settings.api_key_list(),
        rag,
        issues,
        questionnaire,
        web=web,
        default_tz=sched.default_tz,
        background=background,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.listen_port,
    )


if __name__ == "__main__":
    main()
