#!/usr/bin/env bash
# Register Uriel's Nextcloud webhooks through the webhook_listeners OCS API, as a Nextcloud admin.
# Run once per environment: running it again registers duplicates (list them with a GET on the same URL).
#   NC_URL=https://cloud.example.com NC_ADMIN=anthony-headband NC_ADMIN_APP_PASSWORD=... \
#   URIEL_EVENTS_URL=http://uriel-events.uriel.svc.cluster.local:8002 URIEL_EVENTS_KEY=... \
#   scripts/register-nextcloud-webhooks.sh
set -euo pipefail
: "${NC_URL:?}" "${NC_ADMIN:?}" "${NC_ADMIN_APP_PASSWORD:?}" "${URIEL_EVENTS_URL:?}" "${URIEL_EVENTS_KEY:?}"
for event in NodeCreatedEvent NodeWrittenEvent NodeDeletedEvent NodeRenamedEvent; do
  body=$(python3 -c 'import json, sys; print(json.dumps({
      "httpMethod": "POST", "uri": sys.argv[1] + "/events/nextcloud",
      "event": "OCP\\Files\\Events\\Node\\" + sys.argv[2],
      "authMethod": "header", "authData": {"X-Uriel-Source-Key": sys.argv[3]}}))' \
    "$URIEL_EVENTS_URL" "$event" "$URIEL_EVENTS_KEY")
  curl -fsS -u "$NC_ADMIN:$NC_ADMIN_APP_PASSWORD" -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
    -X POST "$NC_URL/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks?format=json" -d "$body" >/dev/null
  echo "registered $event"
done
