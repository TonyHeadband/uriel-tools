import logging
from pathlib import PurePosixPath

from psycopg_pool import AsyncConnectionPool

from uriel_tools.gating import Caller
from uriel_tools.rag import store
from uriel_tools.rag.sources import normalise
from uriel_tools.rag.store import vector_literal

log = logging.getLogger("uriel_tools.search")

MAX_K = 10
CANDIDATES = 40
RRF_K = 60
SNIPPET_CHARS = 600

# Access control lives in `visible`, which every other part reads from. Vectors from another embedding
# model are not comparable, so stale chunks only take part through full-text until the indexer re-embeds them.
# `visible` is referenced by both `vec` and `txt` below, so it must stay materialised (Postgres CTEs
# materialise once referenced more than once): the vector half is then an exact scan restricted to the
# caller's own chunks. Inlining it would let HNSW post-filter the caller's rows, which cuts recall for
# users with small folders.
SEARCH_SQL = f"""
WITH visible AS (
  SELECT d.id, d.path, d.fileid, d.modified, f.mount AS folder,
         CASE WHEN f.owners && %(ids)s::text[] THEN NULL ELSE f.shared_by END AS shared_by,
         e.source_path AS edit_of, e.status AS edit_status
  FROM documents d JOIN folders f ON f.id = d.folder_id
  LEFT JOIN edits e ON e.folder_id = d.folder_id AND e.output_path = d.path
  WHERE (f.owners && %(ids)s::text[] OR f.share_with && %(groups)s::text[])
    AND (%(folder)s::text IS NULL OR f.mount = %(folder)s)
),
vec AS (
  SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(qvec)s::vector) AS r
  FROM chunks c JOIN visible v ON v.id = c.document_id
  WHERE %(qvec)s::text IS NOT NULL AND c.embed_model = %(model)s
  ORDER BY c.embedding <=> %(qvec)s::vector
  LIMIT {CANDIDATES}
),
txt AS (
  SELECT c.id, row_number() OVER (ORDER BY ts_rank(c.tsv, q) DESC) AS r
  FROM chunks c JOIN visible v ON v.id = c.document_id, websearch_to_tsquery('simple', %(query)s) q
  WHERE c.tsv @@ q
  ORDER BY ts_rank(c.tsv, q) DESC
  LIMIT {CANDIDATES}
),
fused AS (
  SELECT id, sum(1.0 / ({RRF_K} + r)) AS score
  FROM (SELECT * FROM vec UNION ALL SELECT * FROM txt) u GROUP BY id
),
ranked AS (
  SELECT c.page, c.source, c.text, v.path, v.fileid, v.modified, v.folder, v.shared_by, v.edit_of,
         v.edit_status, fu.score,
         row_number() OVER (PARTITION BY c.document_id ORDER BY fu.score DESC) AS per_doc
  FROM fused fu JOIN chunks c ON c.id = fu.id JOIN visible v ON v.id = c.document_id
)
SELECT * FROM ranked WHERE per_doc <= 2 ORDER BY score DESC, path LIMIT %(k)s
"""


async def search(
    pool: AsyncConnectionPool,
    embedder,
    caller: Caller,
    query: str,
    k: int = 5,
    folder: str | None = None,
    public_url: str = "",
) -> dict:
    if not caller.user:
        return {"hits": [], "awaiting_ocr": 0}
    degraded = None
    try:
        [qvec] = await embedder.embed([query], query=True)
        qtext = vector_literal(qvec)
    except Exception as e:
        # Any failure degrades to text-only: a misbehaving model server must not take search down.
        # Never log the message, query or URL, only the exception type.
        log.warning("query embedding failed: %s", type(e).__name__)
        qtext, degraded = None, "semantic search unavailable"
    params = {
        "ids": caller.nextcloud_ids,
        "groups": sorted(caller.groups),
        "folder": normalise(folder) if folder else None,
        "qvec": qtext,
        "model": embedder.model,
        "query": query,
        "k": max(1, min(k, MAX_K)),
    }
    async with pool.connection() as conn:
        rows = await (await conn.execute(SEARCH_SQL, params)).fetchall()
    # The rerank hook goes here once a rerank server exists (spec R9); until then RRF order is final.
    base = public_url.rstrip("/")
    hits = [
        {
            "title": PurePosixPath(r["path"]).name,
            "folder": r["folder"],
            **({"shared_by": r["shared_by"]} if r["shared_by"] else {}),
            **({"edit_of": r["edit_of"], "edit_status": r["edit_status"]} if r["edit_of"] else {}),
            "path": r["path"],
            "page": r["page"],
            "source": r["source"],
            "snippet": r["text"][:SNIPPET_CHARS],
            "modified": r["modified"].date().isoformat() if r["modified"] else None,
            "url": f"{base}/f/{r['fileid']}" if base and r["fileid"] else None,
        }
        for r in rows
    ]
    result = {
        "hits": hits,
        "awaiting_ocr": await store.awaiting_ocr_count(pool, caller.nextcloud_ids, caller.groups),
    }
    if degraded:
        result["degraded"] = degraded
    return result
