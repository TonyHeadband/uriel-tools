# Document-RAG verify-first probes (2026-09-27)

Findings from running the plan's "verify first" probes against the cluster Ollama
(`http://192.168.1.10:31434`) before Task 8/13 are implemented. One section per step.

## Step 1: Pull the embedding model on the cluster Ollama

Command:

```bash
curl -s http://192.168.1.10:31434/api/pull -d '{"model":"qwen3-embedding:0.6b","stream":false}'
curl -s http://192.168.1.10:31434/api/tags | python3 -c 'import json,sys; print([m["name"] for m in json.load(sys.stdin)["models"]])'
```

Observed:

```
{"status":"success"}
['qwen3-embedding:0.6b', 'qwen3-vl:8b', 'hf.co/mradermacher/Magistry-24B-v1.1-GGUF:Q2_K', 'reefer/erphermesl3:latest', 'gemma4:e4b', 'nomic-embed-text:latest', 'qwen3.5:9b', 'qwen3:8b', 'deepseek-r1:8b']
```

Consequence: none. Both `qwen3-embedding:0.6b` and `qwen3-vl:8b` are present on the cluster
Ollama, matching the plan's assumption. Pull completed in well under a minute (small model),
no generous timeout was actually needed.

## Step 2: Probe embeddings and the `dimensions` parameter

Command:

```bash
for d in 1024 256; do curl -s http://192.168.1.10:31434/v1/embeddings \
  -d "{\"model\":\"qwen3-embedding:0.6b\",\"input\":[\"bollettino della caldaia\"],\"dimensions\":$d}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(len(r["data"][0]["embedding"]) if "data" in r else r)'; done
```

Observed:

```
1024
256
```

Consequence: none. `dimensions` is honoured server-side (1024 then 256, not 1024 twice). Task
8's client-side truncation/normalisation is still safe to keep as a defence-in-depth measure,
but it is not the thing guaranteeing the embedding size — the server already returns the
requested width.

## Step 3: Probe Qwen3-VL image input, then unloading

Per controller ruling, ran the probe with `uv run --with pillow --with httpx` instead of `uv add
pillow`. Note: `--with httpx` was redundant — `uv run --with pillow` alone already includes
`httpx` from the dev dependency group (`uv run` includes dev-group deps by default), verified
separately with `uv run --with pillow python -c "import httpx; print(httpx.__version__)"` →
`0.28.1`. Command as actually run:

```bash
uv run --with pillow --with httpx python - <<'EOF'
import base64, io, json, time, httpx
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (900, 200), "white")
ImageDraw.Draw(img).text((20, 60), "Bolletta n. 4471 - scadenza 12/03/2026", fill="black", font=ImageFont.load_default(size=36))
buf = io.BytesIO(); img.save(buf, "PNG")
url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
body = {"model": "qwen3-vl:8b", "temperature": 0, "reasoning_effort": "none", "messages": [{"role": "user", "content": [
    {"type": "text", "text": "Transcribe all text in this image exactly as written. Output only the transcription."},
    {"type": "image_url", "image_url": {"url": url}}]}]}
t = time.time(); r = httpx.post("http://192.168.1.10:31434/v1/chat/completions", json=body, timeout=600)
print(round(time.time() - t, 1), "s", json.dumps(r.json()["choices"][0]["message"], ensure_ascii=False)[:400])
EOF
curl -s http://192.168.1.10:31434/api/ps | python3 -m json.tool | grep -E '"name"|size_vram'
curl -s http://192.168.1.10:31434/api/generate -d '{"model":"qwen3-vl:8b","keep_alive":0}' >/dev/null
curl -s http://192.168.1.10:31434/api/ps | python3 -m json.tool | grep -E '"name"|size_vram'
```

Observed:

```
29.5 s {"role": "assistant", "content": "Bolletta n. 4471 - scadenza 12/03/2026", "reasoning": "<think>\nGot it, let's look at the image. The text is \"Bolletta n. 4471 - scadenza 12/03/2026\". I need to transcribe it exactly as written. Check each part: \"Bolletta\" with capital B, then \"n.\" which is a period after n, then \"4471\", a hyphen, \"scadenza\", then the date 12/03/2026. Make sure there are

--- ps before unload ---
            "name": "qwen3-vl:8b",
            "size_vram": 5534897929,

--- issuing unload (keep_alive: 0) ---
(no output, as expected — /api/generate with keep_alive:0 and no prompt returns an empty body)

--- ps after unload ---
{"models":[]}
```

Consequence:
- **OCR works**: `content` is exactly `"Bolletta n. 4471 - scadenza 12/03/2026"` — contains
  both `4471` and `12/03/2026` verbatim, and `content` itself has no thinking tags. None.
- **`reasoning_effort: none` does not fully suppress reasoning**: the response still carries a
  separate `reasoning` field populated with a full `<think>...</think>` block, even though
  `content` is clean. If Task 7/8's parsing only reads `message["content"]` (as the plan
  assumes), this is harmless — but if any code does `json.dumps(message)` or otherwise
  serialises the full message object (e.g. for logging/caching), the `reasoning` field will
  leak a large chain-of-thought blob into logs/storage. **Minor plan note**: Task 7 should
  read only `message["content"]` and should not persist/log the raw `message` object
  wholesale.
- **`keep_alive: 0` unloads the model**: before unload, `api/ps` listed `qwen3-vl:8b` with
  `size_vram: 5534897929` (~5.15 GiB). After the unload call, `api/ps` returned `{"models":
  []}` — the model list is empty, confirming the unload. None.
- VRAM used by `qwen3-vl:8b` while loaded: **~5.15 GiB** (5,534,897,929 bytes). Record this for
  capacity planning against other models sharing the cluster GPU.
- OCR latency for this small synthetic image: ~29.5s end-to-end (cold-ish load; not a
  representative benchmark, single sample).

## Step 4: Record the webhook payload and dispatch facts

These are documentation facts (not a live probe) taken from the Nextcloud admin manual's
`webhook_listeners` app documentation, as instructed by the brief, checked 2026-09-27:

- Envelope: `{"event": {"class": ...}, "user": {"uid", "displayName"}, "time"}`.
- Node events carry `event.node = {"id", "path": "/<uid>/files/<path>"}`.
- Renames carry `event.source` and `event.target` (instead of/alongside `event.node`).
- Dispatch happens from background jobs, every 5 minutes by default (not real-time push).
- Registration: `POST /ocs/v2.php/apps/webhook_listeners/api/v1/webhooks` as an admin, with
  fields `httpMethod`, `uri`, `event`, `eventFilter`, `headers`, `authMethod` (`none`/`header`),
  `authData`.

Consequence: none — these are taken as given per the brief (no live Nextcloud webhook was
registered or fired as part of this task). Task 13's registration script and payload parsing
can proceed on these assumptions. **Not independently verified against a live webhook
delivery** in this task; if Task 13 registers a real webhook, it should capture one real
payload and diff it against this shape before relying on it in production parsing.

## Step 5: Ask the user to check a file link

Pending: user check, asked by the controller. Not performed in this task — requires a human to
open `https://cloud.example.com/f/<fileid>` for one of their own files and report
whether it lands on the file after the Authelia login.

## Overall consequences for the plan

- No assumption was contradicted. `dimensions` is honoured server-side, image OCR through
  `qwen3-vl:8b` correctly reads the embedded numbers/date, `keep_alive: 0` reliably unloads a
  model, and both target models are present on the cluster Ollama.
- One minor addendum for Task 7: don't serialise/log the full chat message object verbatim
  (it can carry a `reasoning` field with an internal `<think>` block even under
  `reasoning_effort: none`); read `message["content"]` only.
- Step 5 remains open pending a human check.
