import json

import httpx
import pytest

from tests.dbutil import folders
from uriel_tools.db import make_pool
from uriel_tools.events.app import MAX_BODY, JobRequest, build_events_app, source_keys
from uriel_tools.events.nextcloud import PayloadError, jobs_for
from uriel_tools.rag.store import Folder

NODE = "OCP\\Files\\Events\\Node\\"
FOLDERS = [
    Folder(1, "/Documents (2)", ("alice",), "Alice", (), "ok"),
    Folder(2, "/Casa", ("alice", "authelia-19e1"), "Alice, Lil-B", ("family",), "ok"),
    Folder(3, "/Scuola", ("authelia-19e1",), "Lil-B", (), "ok"),
    Folder(4, "/Docs", ("carl",), "Carl", (), "auth_failed"),
]


def event(cls, **nodes):
    return {
        "event": {"class": NODE + cls, **{k: {"id": 1, "path": v} for k, v in nodes.items()}},
        "user": {"uid": "x"},
        "time": 1,
    }


def reconciles(*folder_ids):
    return [JobRequest("reconcile", i, "", settle=True) for i in folder_ids]


def test_a_change_by_a_sharing_owner_rechecks_their_shared_folders():
    # The owner's path says nothing about uriel's tree, so every folder they share is re-checked.
    for cls in ("NodeWrittenEvent", "NodeCreatedEvent", "NodeDeletedEvent"):
        assert jobs_for(event(cls, node="/alice/files/Documents/a.pdf"), FOLDERS) == reconciles(1, 2)
    assert jobs_for(event("NodeWrittenEvent", node="/authelia-19e1/files/x.pdf"), FOLDERS) == reconciles(2, 3)


def test_rename_between_owners_rechecks_both():
    moved = event("NodeRenamedEvent", source="/alice/files/a.pdf", target="/authelia-19e1/files/a.pdf")
    assert jobs_for(moved, FOLDERS) == reconciles(1, 2, 3)


def test_non_sharers_non_files_paused_folders_and_other_events_are_ignored():
    assert jobs_for(event("NodeWrittenEvent", node="/bob/files/Documents/a.pdf"), FOLDERS) == []
    assert jobs_for(event("NodeWrittenEvent", node="/alice/files_trashbin/files/a.pdf"), FOLDERS) == []
    assert jobs_for(event("NodeWrittenEvent", node="/carl/files/Docs/a.pdf"), FOLDERS) == []  # auth_failed
    assert jobs_for(event("NodeTouchedEvent", node="/alice/files/Documents/a.pdf"), FOLDERS) == []


@pytest.mark.parametrize(
    "payload", [{}, {"event": {"class": 3}}, {"event": {"class": NODE + "NodeWrittenEvent"}}]
)
def test_malformed_payloads(payload):
    with pytest.raises(PayloadError):
        jobs_for(payload, FOLDERS)


def test_source_keys_from_env():
    assert source_keys({"URIEL_EVENTS_KEY_NEXTCLOUD": "k", "OTHER": "x"}) == {"nextcloud": "k"}
    assert source_keys({"URIEL_EVENTS_KEY_NEXTCLOUD": ""}) == {}


async def post(app, path, body: bytes, key="k"):
    headers = {"content-type": "application/json"} | ({"x-uriel-source-key": key} if key else {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://events") as h:
        return await h.post(path, content=body, headers=headers)


@pytest.mark.db
async def test_intake_status_codes_and_queueing(pool):
    [docs] = await folders(pool, {"owner": "alice", "path": "/Documents"})
    app = build_events_app(pool, {"nextcloud": "k"})
    written = json.dumps(event("NodeWrittenEvent", node="/alice/files/Documents/a.pdf")).encode()
    async with app.router.lifespan_context(app):
        assert (await post(app, "/events/nextcloud", written, key=None)).status_code == 401
        assert (await post(app, "/events/nextcloud", written, key="wrong")).status_code == 401
        assert (await post(app, "/events/frigate", written)).status_code == 404
        assert (await post(app, "/events/nextcloud", b"{not json")).status_code == 422
        assert (await post(app, "/events/nextcloud", b'{"event": {}}')).status_code == 422
        assert (await post(app, "/events/nextcloud", b"x" * (MAX_BODY + 1))).status_code == 413
        for _ in range(3):  # a burst of saves collapses into one delayed reconcile
            r = await post(app, "/events/nextcloud", written)
            assert r.status_code == 202 and r.json() == {"queued": 1}
        stranger = json.dumps(event("NodeWrittenEvent", node="/bob/files/x.jpg")).encode()
        assert (await post(app, "/events/nextcloud", stranger)).json() == {"queued": 0}
        # inside the lifespan: the app's own `pool.close()` on exit would otherwise make this query fail
        async with pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT kind, folder_id, run_after > now() AS delayed FROM jobs")
            ).fetchall()
        assert rows == [{"kind": "reconcile", "folder_id": docs.id, "delayed": True}]


async def test_database_down_is_503():
    down = make_pool("postgresql://nobody@127.0.0.1:1/none", timeout=1)
    await down.open()
    app = build_events_app(down, {"nextcloud": "k"})  # lifespan not entered: it would fail to migrate
    body = json.dumps(event("NodeWrittenEvent", node="/alice/files/Documents/a.pdf")).encode()
    assert (await post(app, "/events/nextcloud", body)).status_code == 503
    await down.close()
