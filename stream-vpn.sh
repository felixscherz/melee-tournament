#!/usr/bin/env bash
#
# Bring the smash WireGuard tunnel up/down on demand.
#
# The Mac joins the 10.0.0.0/24 VPN only while the dashboard needs to be
# public; the rest of the time it stays off the VPN. See docs/DEPLOYMENT.md
# for the full setup.
#
#   ./stream-vpn.sh up       # join the VPN (dashboard goes public)
#   ./stream-vpn.sh down     # leave the VPN
#   ./stream-vpn.sh status   # show handshake / transfer
#
# Note: OBS streams directly to Twitch now (no OME/forwarder shim needed).
# The tunnel is only used to expose the FastAPI dashboard (smash.felixscherz.me).
#
# Requires: brew install wireguard-tools
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${WG_CONF:-$SCRIPT_DIR/config/wireguard/wg0-smash.conf}"
VM_WG_IP="10.0.0.1"
WG_IP="10.0.0.20"                       # this Mac's address on the VPN

if ! command -v wg-quick >/dev/null 2>&1; then
  echo "wg-quick not found. Install with: brew install wireguard-tools" >&2
  exit 1
fi

case "${1:-}" in
  up)
    if ifconfig | grep -q "inet ${WG_IP} "; then
      echo "Tunnel already up (${WG_IP})."
    else
      if [ ! -f "$CONF" ]; then
        echo "Config not found: $CONF" >&2
        echo "Create it per docs/DEPLOYMENT.md (Mac side setup) first." >&2
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
    ;;
  down)
    if ifconfig | grep -q "inet ${WG_IP} "; then
      sudo wg-quick down "$CONF"
      echo "VPN down."
    else
      echo "VPN already down."
    fi
    ;;
  status)
    sudo wg show || true
    ;;
  *)
    echo "usage: $0 {up|down|status}" >&2
    exit 1
    ;;
esac