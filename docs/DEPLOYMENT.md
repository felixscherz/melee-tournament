# Deployment — WireGuard Tunnel + Hetzner Proxy

> **This is one worked example of exposing the dashboard publicly, not a required
> path.** It documents the author's setup. If you're self-hosting, any reverse
> proxy with TLS works (Cloudflare Tunnel, Tailscale Funnel, a VPS with
> nginx/Caddy over a VPN, …) — just point it at `http://<your-mac>:8080` and set
> `[domains] frontend` in `config/settings.toml` to your public hostname. See the
> README's "Going public" section. The VM side below lives in a **separate
> private repo** and can't be reproduced verbatim from this one.

Exposes the local Smash Tournament dashboard (FastAPI) to the internet over a
**WireGuard** tunnel to a Hetzner VM with nginx TLS termination.

**Public URL when live (tunnel up):**
- `https://smash.felixscherz.me` → FastAPI (lobby, watch, API)

The video stream goes directly from OBS to Twitch's CDN — it does not traverse
the tunnel. The Mac uploads one copy to Twitch regardless of viewer count.

The dashboard is **on-demand**: the Mac joins the VPN only while needed, so the
public site serves only while `./stream-vpn.sh up` is active (502 otherwise).

---

## Architecture

```
                       public internet
  browser ─HTTPS──►  nginx (VM) ──► 10.0.0.20:8080          (FastAPI)
                                      │
                           WireGuard wg0  10.0.0.1 ↔ 10.0.0.20
                                      │
                                   [ Mac ]  (tunnel up only while dashboard is public)

  [OBS Studio] ──RTMP──► [Twitch CDN]  (direct; no tunnel involvement)
```

---

## VM side — managed by Ansible (the `home` repo)

The Hetzner VM's WireGuard server, nginx, and TLS are provisioned from the
`home` Ansible repo, **not** from this repo:

| Concern | File in `home` | Deploy |
|---|---|---|
| WG server + Mac peer | `templates/server_wg0.conf.j2` | `ansible-playbook main.yml --limit proxy --tags vpn -i inventory.yaml --ask-vault-pass` |
| nginx upstreams (→ `10.0.0.20`) + TLS | `files/nginx.conf`, `main.yml` | `ansible-playbook main.yml --limit proxy --tags proxy -i inventory.yaml --ask-vault-pass` |

Hetzner firewall must allow inbound **UDP 51820** (WireGuard) plus TCP 22/80/443.

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
4. Write `config/wireguard/wg0-smash.conf` (gitignored):
   ```ini
   [Interface]
   PrivateKey = <contents of mac.privkey>
   Address    = 10.0.0.20/24
   MTU        = 1380

   [Peer]
   PublicKey           = <VM wg0 public key: ssh home.proxy 'sudo wg show wg0 public-key'>
   Endpoint            = home.felixscherz.me:51820
   AllowedIPs          = 10.0.0.0/24
   PersistentKeepalive = 25
   ```
   `AllowedIPs = 10.0.0.0/24` routes **only** the VPN subnet through the tunnel -
   the Mac's normal internet is untouched. Do **not** use `0.0.0.0/0`. Keep
   `MTU 1380` equal on both ends to avoid fragmentation over WireGuard. Bring it
   up with `./stream-vpn.sh up`; the handshake completes once the VM knows the
   peer (`vpn` tag deployed).

---

## Running it (day-to-day)

```bash
./stream-vpn.sh up                       # join VPN (dashboard goes public)
uv run main.py                           # FastAPI + Dolphin
# open OBS → Start Streaming (pushes directly to Twitch)
# ... run matches ...
./stream-vpn.sh down                     # leave VPN when done
```

---

## Rollback

If the tunnel misbehaves, check in order: `./stream-vpn.sh status` (handshake),
`ping 10.0.0.1`, and on the VM `sudo iptables -t nat -S | grep 10.0.0.20`.