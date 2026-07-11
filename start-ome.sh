#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config/ome-Server.xml"
MOUNT_TARGET="/opt/ovenmediaengine/bin/origin_conf/Server.xml"

if ! docker info &>/dev/null; then
  echo "Docker daemon is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  echo "Config not found: $CONFIG" >&2
  exit 1
fi

# In production, OME must advertise the Hetzner public IP in its ICE / TCP-relay
# candidates; on localhost a remote browser can't reach 127.0.0.1. Derive the
# host IP from the streaming mode in settings.toml.
SETTINGS="$SCRIPT_DIR/config/settings.toml"
PUBLIC_IP="78.46.220.137"
STREAM_MODE=$(grep -E '^[[:space:]]*mode[[:space:]]*=' "$SETTINGS" | tail -1 | sed -E 's/.*=[[:space:]]*"?([a-z]+)"?.*/\1/')
if [ "$STREAM_MODE" = "production" ]; then
  OME_HOST_IP="$PUBLIC_IP"
else
  OME_HOST_IP="127.0.0.1"
fi
echo "Streaming mode: ${STREAM_MODE:-local} — OME_HOST_IP=$OME_HOST_IP"

# Token for OME's REST API (push publishing / Twitch relay). Loopback-only, so
# low risk; override by exporting OME_API_TOKEN. Must match twitch-push.sh.
OME_API_TOKEN="${OME_API_TOKEN:-smash-ome-api}"

needs_recreate=false

if docker inspect ome &>/dev/null; then
  # Check whether the container already has the config bind-mounted
  mounted=$(docker inspect ome --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' | grep -c "$MOUNT_TARGET" || true)
  if [ "$mounted" -eq 0 ]; then
    echo "Container 'ome' exists but is missing the config bind mount — recreating it."
    docker rm -f ome
    needs_recreate=true
  fi
  # OME_HOST_IP is baked in at run time; a plain 'docker start' keeps the old
  # value. If the desired host IP changed (e.g. local↔production), recreate.
  current_ip=$(docker inspect ome --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -nE 's/^OME_HOST_IP=//p')
  if [ "$needs_recreate" = false ] && [ "$current_ip" != "$OME_HOST_IP" ]; then
    echo "Container 'ome' has OME_HOST_IP=$current_ip but need $OME_HOST_IP — recreating it."
    docker rm -f ome
    needs_recreate=true
  fi
else
  needs_recreate=true
fi

# OME must bind its loopback ports before the forwarder shim binds the 0.0.0.0
# wildcard, or 'docker run' fails with "address already in use" on 10000-10004.
# If the shim is up, stop it here; ./stream-vpn.sh up restarts it afterwards.
if pgrep -f stream_forwarder.py >/dev/null 2>&1; then
  echo "⚠ Stopping the OME forwarder shim to free 10000-10004 for the container."
  echo "  Re-run './stream-vpn.sh up' after this to restart the forwarders."
  pkill -f stream_forwarder.py 2>/dev/null || true
  sleep 1
fi

if [ "$needs_recreate" = true ]; then
  echo "Creating container 'ome'."
  # WebRTC signaling (3355) and media (10000-10009/udp) are bound to loopback:
  # the Podman/Docker gvproxy port-forwarder does NOT serve the WireGuard utun
  # interface, so production traffic reaches OME via the native forwarder shim in
  # stream-vpn.sh (10.0.0.20 -> 127.0.0.1). Local mode hits these on loopback
  # directly. RTMP 1935 stays on all interfaces (OBS ingests over localhost).
  docker run -d --name ome \
    -p 1935:1935 \
    -p 127.0.0.1:3355:3333 \
    -p 127.0.0.1:8081:8081 \
    -p 127.0.0.1:10000-10009:10000-10009/udp \
    -e OME_HOST_IP="$OME_HOST_IP" \
    -e OME_API_TOKEN="$OME_API_TOKEN" \
    -v "$CONFIG:$MOUNT_TARGET:ro" \
    airensoft/ovenmediaengine:latest
else
  echo "Container 'ome' already exists with correct config — starting it."
  docker start ome
fi

echo "Waiting for OME to be ready..."
sleep 3
for i in $(seq 1 30); do
  if docker logs ome 2>&1 | grep -q "All modules are initialized successfully"; then
    echo "OvenMediaEngine is up."
    exit 0
  fi
  sleep 2
done

echo "OME did not report ready within 63s. Check: docker logs ome" >&2
exit 1

