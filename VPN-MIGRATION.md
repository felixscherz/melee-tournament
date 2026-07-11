# Migrating the stream from frp → WireGuard (native UDP)

## Why

Production streaming currently runs over an **frp tunnel** with WebRTC media
forced onto a **TURN/TCP relay** (`TcpForce=true`, port 3478). TCP relay adds
head-of-line blocking — one lost packet stalls the whole video — which is wrong
for a realtime fighting-game stream.

This migration replaces the Mac↔VM link with **WireGuard** so WebRTC media flows
as **native UDP**, NAT'd in-kernel on the VM (fast, no userspace mangling), and
retires frp entirely.

### Decisions (locked)

- **Mac WireGuard is a local, on-demand config** — the server side (peer +
  forwarding rules) lives in the `home` Ansible repo; the Mac gets a one-off
  `wg-quick` config whose private key stays on the Mac, out of git.
- **Pure UDP, no TCP relay fallback** — lowest latency. Viewers on UDP-blocking
  networks won't connect, which is acceptable for the team.
- **On-demand tunnel** — the Mac joins the `10.0.0.0/24` VPN only while
  streaming, via `./stream-vpn.sh up` / `down`. The public site therefore only
  serves while the tunnel is up (expected — streams are on-demand).

## Target architecture

```
                       public internet
 browser ─WSS 443─►  nginx (VM) ──► 10.0.0.20:3355          (OME signaling)
 browser ─HTTPS──►  nginx (VM) ──► 10.0.0.20:8080          (FastAPI)
 browser ─UDP 10000-10004─► VM eth0 ─DNAT+MASQ─► 10.0.0.20:10000-10004 (OME media)
                                     │
                          WireGuard wg0  10.0.0.1 ↔ 10.0.0.20
                                     │
                                  [ Mac ]  (tunnel up only while streaming)
```

The VM stays the public TLS edge; viewers are on the open internet, **not** on
the VPN. WireGuard only replaces the Mac↔VM hop.

### The one critical detail — NAT symmetry for media

The browser sends UDP to `78.46.220.137:10004`; for ICE to succeed OME's replies
must appear to come **back from that same IP:port**. On the VM this requires
**DNAT *and* MASQUERADE**:

- **DNAT** rewrites destination → `10.0.0.20:10004` (crosses wg0 to the Mac).
- **MASQUERADE** rewrites source → `10.0.0.1` (VM's wg IP), so OME replies *to the
  VM* over wg0, where conntrack reverses both translations and emits the packet
  to the browser as `78.46.220.137:10004`.

Without the MASQUERADE, OME would reply to the browser directly over the Mac's
own internet (asymmetric) and ICE fails. MASQUERADE is what avoids routing the
Mac's whole internet through the VM. (OME will see every viewer as `10.0.0.1` —
fine for us.)

### The second critical detail — gvproxy can't serve the VPN, so a shim does

**OME runs under Podman/Docker, whose `gvproxy` port-forwarder does NOT serve the
WireGuard `utun` interface** — not on the wildcard bind, not even bound to the
Mac's VPN IP `10.0.0.20`. Packets arriving on `utun` for a Docker-published port
are silently dropped. (This is *why frp worked*: `frpc` was a native Mac process
that reached OME over `127.0.0.1` and tunneled out. WireGuard has no such agent —
it expects the VM to reach OME directly over the tunnel.)

The fix is `stream_forwarder.py`, a native forwarder that reprises the `frpc`
role: it binds `0.0.0.0` (a plain Python socket *does* receive `utun` traffic —
`socat` notably does not on macOS) and relays to OME on `127.0.0.1`, where
gvproxy works:

```
VM ─wg0─► 10.0.0.20:3355         (stream_forwarder.py) ─► 127.0.0.1:3355 ─gvproxy─► OME
VM ─wg0─► 10.0.0.20:10000-10004  (stream_forwarder.py) ─► 127.0.0.1:1000x ─gvproxy─► OME
```

So OME must publish those ports on `127.0.0.1` (not the wildcard) — see Phase 4.
`stream-vpn.sh up/down` starts/stops the forwarder alongside the tunnel.

A cleaner long-term option: **Apple `container`** (macOS 26) gives each container
its own host-routable vmnet IP with no gvproxy hop, which would remove the shim
— but it's a larger runtime/image migration, deferred for now.

---

## Repos touched

| Change | Repo / file |
|---|---|
| Mac peer + media DNAT | `~/workspaces/personal/home` → `templates/server_wg0.conf.j2` |
| nginx upstreams → VPN | `~/workspaces/personal/home` → `files/nginx.conf` |
| OME to UDP + dedicated port | `smash-tournament` → `config/ome-Server.xml`, `start-ome.sh`, `config/settings.toml`, lobby template |
| On-demand VPN script | `smash-tournament` → `stream-vpn.sh`, `config/wireguard/wg0-smash.conf` (gitignored) |
| Delete frp | `smash-tournament` → `config/frpc.toml`, `config/frps.toml` + docs |

frp is **not** managed by Ansible (`frps` was hand-installed on the VM), so
removing it is manual: stop the service on the VM, kill `frpc` on the Mac.

---

## Phase 0 — Prerequisites

- [ ] `brew install wireguard-tools` on the Mac (`wg`, `wg-quick`).
- [ ] Ansible working locally against the `proxy` host (vault password on hand).
- [ ] Confirm the VM WG endpoint DNS: `home.felixscherz.me` → `78.46.220.137`.
- [ ] Hetzner firewall already allows inbound **UDP 51820** (WG) and **UDP
      10000-10004** (opened during the frp work) — no new rules needed.

## Phase 1 — WireGuard keypair + Mac config + on-demand script

1. Generate the Mac keypair (private key never leaves the Mac):
   ```bash
   mkdir -p ~/workspaces/personal/smash-tournament/config/wireguard
   cd ~/workspaces/personal/smash-tournament/config/wireguard
   wg genkey | tee mac.privkey | wg pubkey > mac.pubkey
   chmod 600 mac.privkey
   ```
2. Fetch the VM's WG public key (not secret):
   ```bash
   ssh home.proxy 'sudo wg show wg0 public-key'
   ```
3. Write `config/wireguard/wg0-smash.conf` (gitignored):
   ```ini
   [Interface]
   PrivateKey = <contents of mac.privkey>
   Address    = 10.0.0.20/24
   MTU        = 1380

   [Peer]
   PublicKey           = <VM wg0 public key from step 2>
   Endpoint            = home.felixscherz.me:51820
   AllowedIPs          = 10.0.0.0/24
   PersistentKeepalive = 25
   ```
   `AllowedIPs = 10.0.0.0/24` routes **only** the VPN subnet through the tunnel —
   the Mac's normal internet is untouched. Do **not** use `0.0.0.0/0`.
4. Bring it up with the helper script (see `stream-vpn.sh`):
   ```bash
   ./stream-vpn.sh up
   ```
   At this point the peer exists only on the Mac; the VM doesn't know it yet, so
   the handshake won't complete until Phase 2. That's expected.

**Verify (after Phase 2):** `./stream-vpn.sh status` shows a recent handshake;
`ping -c3 10.0.0.1` succeeds.

## Phase 2 — Ansible: add Mac peer + media forwarding (VM)

Edit `templates/server_wg0.conf.j2`:

1. Add the peer (plaintext pubkey, like the others):
   ```jinja
   [Peer]
   # mac (smash streaming)
   PublicKey = <contents of mac.pubkey>
   AllowedIPs = 10.0.0.20/32
   ```
2. Append media DNAT to `PostUp` and mirror in `PostDown` (`-A`→`-D`):
   ```
   PostUp = iptables -t nat -A PREROUTING -i {{ proxy.external_interface }} -p udp --dport 10000:10004 -j DNAT --to-destination 10.0.0.20
   PostUp = iptables -t nat -A POSTROUTING -o %i -p udp -d 10.0.0.20 --dport 10000:10004 -j MASQUERADE
   PostDown = iptables -t nat -D PREROUTING -i {{ proxy.external_interface }} -p udp --dport 10000:10004 -j DNAT --to-destination 10.0.0.20
   PostDown = iptables -t nat -D POSTROUTING -o %i -p udp -d 10.0.0.20 --dport 10000:10004 -j MASQUERADE
   ```
   The existing `PostUp` already opens FORWARD both directions, so no extra
   FORWARD rule is needed.
3. Deploy:
   ```bash
   cd ~/workspaces/personal/home
   ansible-playbook main.yml --limit proxy --tags vpn -i inventory.yaml --ask-vault-pass --check   # dry run
   ansible-playbook main.yml --limit proxy --tags vpn -i inventory.yaml --ask-vault-pass           # apply
   ```
   (The VPN-server play is tagged `vpn`, not `wireguard`. The `Restart wireguard`
   handler re-applies `wg0` and its PostUp rules.)

**Verify:** on the Mac `./stream-vpn.sh status` shows a handshake; `ping 10.0.0.1`
works; on the VM `sudo iptables -t nat -S | grep 10.0.0.20` shows the DNAT+MASQ.

## Phase 3 — Ansible: repoint nginx onto the VPN (VM)

Edit `files/nginx.conf`:

- `smash.felixscherz.me` — `/` and `/ws/`: `http://127.0.0.1:8080` → `http://10.0.0.20:8080`
- `stream-smash.felixscherz.me` — `/`: `http://127.0.0.1:13333` → `http://10.0.0.20:3355`

Deploy (the reverse-proxy play is tagged `proxy`):
```bash
ansible-playbook main.yml --limit proxy --tags proxy -i inventory.yaml --ask-vault-pass --check   # dry run
ansible-playbook main.yml --limit proxy --tags proxy -i inventory.yaml --ask-vault-pass           # triggers Restart nginx
```

**Verify (VPN up):** from the VM, `curl -I http://10.0.0.20:8080/` reaches
FastAPI. (Signaling on 3355 comes online in Phase 4.)

## Phase 4 — Smash: OME to native UDP + dedicated signaling port

In `smash-tournament`:

1. `config/ome-Server.xml`: set both `<TcpForce>true</TcpForce>` back to
   **`false`** (native UDP host candidates; `OME_HOST_IP` stays the public IP so
   candidates advertise `78.46.220.137:10000-10004`).
2. `start-ome.sh`: publish signaling on a dedicated host port to end the Obsidian
   `127.0.0.1:3333` collision, and bind the WebRTC ports to **loopback** so the
   forwarder shim (see "second critical detail") can own the VPN IP without a
   bind conflict:
   - `-p 3333:3333`               → `-p 127.0.0.1:3355:3333`
   - `-p 10000-10009:...:/udp`    → `-p 127.0.0.1:10000-10009:10000-10009/udp`
   - RTMP `1935` stays on all interfaces (OBS ingests over localhost either way).
3. Update local-mode references to `3355`:
   - `config/settings.toml` → `webrtc_signal = "ws://localhost:3355/app/stream"`
   - `frontend/templates/lobby.html` health-check → `ws://localhost:3355/app/stream`
4. Recreate OME, then (re)start the forwarder shim:
   ```bash
   docker rm -f ome && ./start-ome.sh
   ./stream-vpn.sh up          # (re)starts stream_forwarder.py alongside the tunnel
   ```

**Verify:** `curl http://127.0.0.1:3355/app/stream` locally returns OME (a `404`
from OME, not the Obsidian app = correct); from the VM (VPN up, shim running)
`curl http://10.0.0.20:3355/app/stream` also returns OME's `404`.

## Phase 5 — Cutover test (UDP media)

With the VPN up and OME recreated:

1. Hard-reload `https://smash.felixscherz.me/watch` (Cmd+Shift+R).
2. In `chrome://webrtc-internals`, confirm the **selected candidate pair is a
   UDP host pair on `78.46.220.137:1000x`** — **not** `relay`.
3. In OME logs, `docker logs ome | grep "uses candidate"` shows the UDP pair.
4. Sanity-check latency vs. the old TCP relay (should be visibly lower).

**Rollback (any time before Phase 6):** frp is still running. Revert
`files/nginx.conf` to `127.0.0.1:13333`/`8080` + re-run the playbook, set
`TcpForce=true`, `docker rm -f ome && ./start-ome.sh`, ensure `frpc` is up — back
to the relay setup in ~2 minutes.

## Phase 6 — Retire frp

Once UDP is confirmed:

1. **Mac:** stop `frpc` (`pkill -f 'frpc -c config/frpc.toml'`); remove any
   autostart.
2. **VM:** `sudo systemctl disable --now frps` (or kill the process / remove the
   unit + binary if hand-installed).
3. **Repo:** delete `config/frpc.toml`, `config/frps.toml`; strip the frp / TCP
   relay sections from `CLAUDE.md`, `DEPLOYMENT.md`, `docs/setup.md`.
4. Optionally remove the now-unused `3478` and `10000-10009` docker relay
   plumbing you don't need (keep `10000-10004/udp` — that's the media path).

---

## Operating it day-to-day

```bash
./stream-vpn.sh up      # join the VPN before streaming
# ... run start-ome.sh, python main.py, OBS, stream ...
./stream-vpn.sh down    # leave the VPN when done
```

While the tunnel is down, `smash.felixscherz.me` returns 502 — that's expected;
bring the VPN up to serve.

## Notes / gotchas

- **MTU 1380** on both ends avoids fragmentation over WireGuard; keep them equal.
- **On-demand DNAT is harmless when down** — with the tunnel down, packets to
  `10000-10004` get DNAT'd toward an unreachable `10.0.0.20` and dropped.
- The Obsidian `127.0.0.1:3333` collision is permanently sidestepped by the
  dedicated `3355` OME port, so the `::1` frpc workaround is no longer needed.
- See project memory `project_production_streaming_frp.md` for the debugging
  history that led here.
```
