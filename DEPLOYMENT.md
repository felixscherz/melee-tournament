# Deployment — WireGuard Tunnel + Hetzner Proxy

Exposes the local Smash Tournament stack to the internet over a **WireGuard**
tunnel to a Hetzner VM with nginx TLS termination. WebRTC media flows as native
UDP (no TCP relay). This replaced the earlier frp tunnel; the one-time migration
write-up and rationale live in `VPN-MIGRATION.md`.

**Public URLs when live (tunnel up):**
- `https://smash.felixscherz.me` → FastAPI (lobby, watch, API)
- `wss://stream-smash.felixscherz.me` → OvenMediaEngine WebRTC signaling

The stream is **on-demand**: the Mac joins the VPN only while streaming, so the
public site serves only while `./stream-vpn.sh up` is active (502 otherwise).

---

## Architecture

```
                       public internet
 browser ─WSS 443─►  nginx (VM) ──► 10.0.0.20:3355          (OME signaling)
 browser ─HTTPS──►  nginx (VM) ──► 10.0.0.20:8080          (FastAPI)
 browser ─UDP 10000-10004─► VM eth0 ─DNAT+MASQ─► 10.0.0.20:10000-10004 (OME media)
                                     │
                          WireGuard wg0  10.0.0.1 ↔ 10.0.0.20
                                     │
                                  [ Mac ]  (tunnel + forwarders up only while streaming)
```

Two non-obvious details make this work — read them before changing anything:

1. **NAT symmetry for media.** The VM does **DNAT *and* MASQUERADE** on UDP
   `10000-10004` so OME's replies traverse the VM (not the Mac's own internet),
   or ICE fails. See `VPN-MIGRATION.md` → "NAT symmetry for media".
2. **The forwarder shim.** OME runs under Podman/Docker, whose gvproxy does
   **not** serve the WireGuard interface. `stream_forwarder.py` (started by
   `stream-vpn.sh`) bridges `10.0.0.20` → OME on `127.0.0.1`. OME therefore
   publishes `3355` + `10000-10004` on loopback. See `VPN-MIGRATION.md` →
   "gvproxy can't serve the VPN".

---

## VM side — managed by Ansible (the `home` repo)

The Hetzner VM's WireGuard server, nginx, and TLS are provisioned from the
`home` Ansible repo, **not** from this repo:

| Concern | File in `home` | Deploy |
|---|---|---|
| WG server + Mac peer + media DNAT/MASQ | `templates/server_wg0.conf.j2` | `ansible-playbook main.yml --limit proxy --tags vpn -i inventory.yaml --ask-vault-pass` |
| nginx upstreams (→ `10.0.0.20`) + TLS | `files/nginx.conf`, `main.yml` | `ansible-playbook main.yml --limit proxy --tags proxy -i inventory.yaml --ask-vault-pass` |

Hetzner firewall must allow inbound **UDP 51820** (WireGuard) and **UDP
10000-10004** (WebRTC media), plus TCP 22/80/443.

---

## Mac side — one-time WireGuard setup

1. `brew install wireguard-tools`
2. Generate the Mac keypair (private key never leaves the Mac; gitignored):
   ```bash
   mkdir -p config/wireguard && cd config/wireguard
   wg genkey | tee mac.privkey | wg pubkey > mac.pubkey
   chmod 600 mac.privkey
   ```
3. Add the Mac's public key (`mac.pubkey`) as a peer in the `home` repo's
   `server_wg0.conf.j2` (`AllowedIPs = 10.0.0.20/32`) and deploy the `vpn` tag.
4. Write `config/wireguard/wg0-smash.conf` (gitignored) — `[Interface]` with the
   private key + `Address = 10.0.0.20/24`, MTU `1380`; `[Peer]` with the VM's
   `wg0` public key, `Endpoint = home.felixscherz.me:51820`,
   `AllowedIPs = 10.0.0.0/24` (subnet only — not `0.0.0.0/0`),
   `PersistentKeepalive = 25`. Full template in `VPN-MIGRATION.md` (Phase 1).

---

## Running it (day-to-day)

```bash
./start-ome.sh              # start OvenMediaEngine FIRST (binds loopback ports)
./stream-vpn.sh up          # THEN join VPN + start the forwarder shim
source .venv/bin/activate && python main.py   # FastAPI + Dolphin
# open OBS → Start Streaming
# ... run matches ...
./stream-vpn.sh down        # leave VPN + stop forwarders when done
```

**Order matters:** start OME before the shim. OME binds `10000-10004` on
loopback; the shim binds the same ports on `0.0.0.0`. If the shim is up first,
`docker run` fails with "address already in use". `start-ome.sh` will stop a
running shim to recreate the container — just re-run `./stream-vpn.sh up` after.

Set `mode = "production"` in `config/settings.toml` so the watch page embeds
`wss://stream-smash.felixscherz.me`.

### Scaling to many viewers — relay to Twitch

The WebRTC path fans out **per viewer from the Mac**, so it's capped by the
Mac's home upload (~N × bitrate; ≈30 viewers × 6 Mbps ≈ 180 Mbps — too much).
For a crowd, offload fan-out to Twitch's CDN — the Mac then uploads only one copy:

```bash
echo "<your-twitch-stream-key>" > config/twitch.key   # gitignored
./twitch-push.sh start      # OME relays app/stream to Twitch (H264+AAC passthrough)
./twitch-push.sh status     # list active pushes
./twitch-push.sh stop
```

Requires OME running (API on `127.0.0.1:8081`) and OBS streaming into OME. The
low-latency WebRTC path keeps working alongside the Twitch relay; use WebRTC for
small groups and the Twitch link when many people watch. Twitch latency is
~2-5s (low-latency mode) vs sub-second WebRTC.

**Verify the path from another machine:** open
`https://smash.felixscherz.me/lobby`, start a match, open the watch page, and in
`chrome://webrtc-internals` confirm the selected candidate pair is a **UDP `host`
pair on `78.46.220.137:1000x`** (not `relay`).

---

## Rollback

There is no frp fallback anymore. If the tunnel misbehaves, check in order:
`./stream-vpn.sh status` (handshake + forwarder alive), `ping 10.0.0.1`, and on
the VM `sudo iptables -t nat -S | grep 10.0.0.20` (DNAT+MASQ present).
