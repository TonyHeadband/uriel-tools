#!/usr/bin/env bash
# Pretend to be Nextcloud against a local events intake (webhooks can't reach localhost from the cluster).
#   scripts/send-test-webhook.sh <nextcloud_user> </path/in/their/files> [Written|Created|Deleted]
set -euo pipefail
user=$1 path=$2 kind=${3:-Written}
body=$(python3 -c 'import json, sys; print(json.dumps({
    "event": {"class": "OCP\\Files\\Events\\Node\\Node" + sys.argv[3] + "Event",
              "node": {"id": 0, "path": "/" + sys.argv[1] + "/files" + sys.argv[2]}},
    "user": {"uid": sys.argv[1]}, "time": 0}))' "$user" "$path" "$kind")
curl -fsS -X POST "${URIEL_EVENTS_URL:-http://localhost:8002}/events/nextcloud" \
  -H "X-Uriel-Source-Key: ${URIEL_EVENTS_KEY:-dev-events-key}" -H 'Content-Type: application/json' -d "$body"
echo
