#!/usr/bin/env bash
#
# Bring the smash streaming WireGuard tunnel up/down on demand, plus the native
# forwarder shim that bridges OME to the VPN.
#
# The Mac joins the 10.0.0.0/24 VPN only while streaming; the rest of the time
# it stays off the VPN. See VPN-MIGRATION.md for the full setup.
#
#   ./stream-vpn.sh up       # join the VPN + start OME forwarders before streaming
#   ./stream-vpn.sh down     # stop forwarders + leave the VPN when done
#   ./stream-vpn.sh status   # show handshake / transfer / forwarders
#
# Why the forwarders: OME runs under Podman/Docker, whose gvproxy port-forwarder
# does NOT serve the WireGuard utun interface. So the VM cannot reach OME's
# published ports directly over the tunnel. stream_forwarder.py binds 0.0.0.0
# (which *does* receive tunnel traffic) and relays to OME on loopback
# (127.0.0.1), where gvproxy works. This is the role frpc used to play. OME must
# publish 3355 + 10000-10004 on 127.0.0.1 (see start-ome.sh).
#
# Requires: brew install wireguard-tools   (forwarder uses stdlib python3 only)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${WG_CONF:-$SCRIPT_DIR/config/wireguard/wg0-smash.conf}"
VM_WG_IP="10.0.0.1"
WG_IP="10.0.0.20"                       # this Mac's address on the VPN
FORWARDER="$SCRIPT_DIR/stream_forwarder.py"
PIDFILE="$SCRIPT_DIR/config/wireguard/.forwarders.pid"

if ! command -v wg-quick >/dev/null 2>&1; then
  echo "wg-quick not found. Install with: brew install wireguard-tools" >&2
  exit 1
fi

start_forwarders() {
  stop_forwarders   # never double-start
  python3 "$FORWARDER" >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  sleep 1
  if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "✓ OME forwarders up (pid $(cat "$PIDFILE"): 3355/tcp + 10000-10004/udp -> 127.0.0.1)."
  else
    echo "⚠ Forwarder failed to start. Try: python3 $FORWARDER" >&2
    rm -f "$PIDFILE"
  fi
}

stop_forwarders() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  # belt-and-suspenders: reap any stray forwarder
  pkill -f "stream_forwarder.py" 2>/dev/null || true
}

case "${1:-}" in
  up)
    if ifconfig | grep -q "inet ${WG_IP} "; then
      echo "Tunnel already up (${WG_IP})."
    else
      if [ ! -f "$CONF" ]; then
        echo "Config not found: $CONF" >&2
        echo "Create it per VPN-MIGRATION.md (Phase 1) first." >&2
        exit 1
      fi
      sudo wg-quick up "$CONF"
    fi
    echo "Verifying handshake to $VM_WG_IP ..."
    if ping -c3 -t3 "$VM_WG_IP" >/dev/null 2>&1; then
      echo "✓ VPN reachable ($VM_WG_IP responds)."
    else
      echo "⚠ Tunnel is up but $VM_WG_IP did not respond." >&2
      echo "  Check the VM knows this peer (Ansible tag: vpn) and firewall UDP 51820." >&2
    fi
    start_forwarders
    ;;
  down)
    stop_forwarders
    echo "Forwarders stopped."
    if ifconfig | grep -q "inet ${WG_IP} "; then
      sudo wg-quick down "$CONF"
      echo "VPN down."
    else
      echo "VPN already down."
    fi
    ;;
  status)
    sudo wg show || true
    echo "--- forwarders ---"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Forwarder alive (pid $(cat "$PIDFILE"): 3355/tcp + 10000-10004/udp)."
    else
      echo "No forwarders running."
    fi
    ;;
  *)
    echo "usage: $0 {up|down|status}" >&2
    exit 1
    ;;
esac
