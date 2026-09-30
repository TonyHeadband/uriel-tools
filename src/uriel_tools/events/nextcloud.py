from uriel_tools.events.app import JobRequest, PayloadError
from uriel_tools.rag.store import Folder

NODE = "OCP\\Files\\Events\\Node\\"
EVENTS = {NODE + n for n in ("NodeCreatedEvent", "NodeWrittenEvent", "NodeDeletedEvent", "NodeRenamedEvent")}


def jobs_for(payload: dict, folders: list[Folder]) -> list[JobRequest]:
    """Map a webhook_listeners payload to jobs.

    Events carry the owner's own path (/<uid>/files/...), which says nothing about where the file sits in
    uriel's tree, so an event from someone who shares folders with uriel re-checks those folders; the
    reconcile finds what changed by etag. Events from anyone else produce nothing and are not stored.
    """
    event = payload.get("event")
    if not isinstance(event, dict) or not isinstance(event.get("class"), str):
        raise PayloadError("missing event.class")
    if event["class"] not in EVENTS:
        return []
    keys = ("source", "target") if event["class"] == NODE + "NodeRenamedEvent" else ("node",)
    owners = {_owner(_path(event, key)) for key in keys} - {None}
    return [
        # Saves arrive in bursts (an editor autosaving, a folder upload): settle, so they fold into one job.
        JobRequest("reconcile", f.id, "", settle=True)
        for f in folders
        if f.state == "ok" and owners & set(f.owners)
    ]


def _path(event: dict, key: str) -> str:
    node = event.get(key)
    if not isinstance(node, dict) or not isinstance(node.get("path"), str):
        raise PayloadError(f"missing event.{key}.path")
    return node["path"]


def _owner(full_path: str) -> str | None:
    # "/<uid>/files/<path>"; anything else (trash bin, versions, app data) is not a user file
    parts = full_path.split("/", 3)
    if len(parts) < 3 or parts[0] != "" or not parts[1] or parts[2] != "files":
        return None
    return parts[1]
