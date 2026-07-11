#!/usr/bin/env bash
#
# Relay the live OME stream to Twitch (or stop it) via OME's push-publishing API.
# This offloads viewer fan-out to Twitch's CDN, so the Mac uploads only one copy
# regardless of viewer count - use it when many people are watching. The
# low-latency WebRTC path keeps working alongside it.
#
#   ./twitch-push.sh start    # start pushing app/stream to Twitch
#   ./twitch-push.sh stop     # stop the push
#   ./twitch-push.sh status   # list active pushes
#
# Requires:
#   - OME running with the API enabled (./start-ome.sh; API on 127.0.0.1:8081)
#   - OBS streaming into OME (so app/stream exists)
#   - Twitch stream key in config/twitch.key (gitignored) or $TWITCH_STREAM_KEY
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API="http://127.0.0.1:8081/v1/vhosts/default/apps/app"
API_TOKEN="${OME_API_TOKEN:-smash-ome-api}"
PUSH_ID="twitch"
# Twitch primary ingest; auto-routes to a nearby server. Override with $TWITCH_URL.
TWITCH_URL="${TWITCH_URL:-rtmp://live.twitch.tv/app}"

AUTH="Authorization: Basic $(printf '%s' "$API_TOKEN" | base64)"

api() {  # api <method> <endpoint-suffix> [json-body]
  local method="$1" suffix="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "${API}${suffix}" -H "$AUTH" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "${API}${suffix}" -H "$AUTH"
  fi
}

load_key() {
  if [ -n "${TWITCH_STREAM_KEY:-}" ]; then
    STREAM_KEY="$TWITCH_STREAM_KEY"
  elif [ -f "$SCRIPT_DIR/config/twitch.key" ]; then
    STREAM_KEY="$(tr -d '[:space:]' < "$SCRIPT_DIR/config/twitch.key")"
  else
    echo "No Twitch key. Put it in config/twitch.key or export TWITCH_STREAM_KEY." >&2
    exit 1
  fi
  [ -n "$STREAM_KEY" ] || { echo "Twitch key is empty." >&2; exit 1; }
}

case "${1:-}" in
  start)
    load_key
    # bypass_video (H264 passthrough) + aac_audio = what Twitch expects; no re-encode.
    body=$(cat <<JSON
{
  "id": "${PUSH_ID}",
  "stream": { "name": "stream", "variantNames": ["bypass_video", "aac_audio"] },
  "protocol": "rtmp",
  "url": "${TWITCH_URL}",
  "streamKey": "${STREAM_KEY}"
}
JSON
)
    echo "Starting Twitch push (id=${PUSH_ID}) -> ${TWITCH_URL}"
    api POST ":startPush" "$body"; echo
    echo "Watch at https://twitch.tv/<your-channel>"
    ;;
  stop)
    echo "Stopping Twitch push (id=${PUSH_ID})"
    api POST ":stopPush" "{\"id\": \"${PUSH_ID}\"}"; echo
    ;;
  status)
    api POST ":pushes"; echo
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 1
    ;;
esac
