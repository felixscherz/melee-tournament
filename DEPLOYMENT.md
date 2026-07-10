# Deployment — frp Tunnel + Hetzner Proxy

Exposes the local Smash Tournament stack to the internet via an frp tunnel
to a Hetzner VM with nginx TLS termination.

**Public URLs when live:**
- `https://smash.felixscherz.me` → FastAPI (lobby, watch, API)
- `wss://stream-smash.felixscherz.me` → OvenMediaEngine WebRTC signaling

---

## Step 1 — Hetzner VM prerequisites

SSH into the VM and install dependencies:

```bash
apt install nginx certbot python3-certbot-nginx
```

Install `frps` at the same version as the local `frpc` (v0.69.1):

```bash
curl -LO https://github.com/fatedier/frp/releases/download/v0.69.1/frp_0.69.1_linux_amd64.tar.gz
tar xf frp_0.69.1_linux_amd64.tar.gz
cp frp_0.69.1_linux_amd64/frps /usr/local/bin/
```

---

## Step 2 — Hetzner firewall rules

Open these ports in the Hetzner Cloud Console (Firewall tab):

| Protocol | Ports |
|---|---|
| TCP | 22, 80, 443, 7000, 3333, 1935 |
| UDP | 10000–10009 |

---

## Step 3 — DNS records

Point both domains at the Hetzner VM's public IP:

```
smash.felixscherz.me        A  <hetzner-ip>
stream-smash.felixscherz.me A  <hetzner-ip>
```

---

## Step 4 — Fill in the shared secret

Choose a strong auth token and update two files:

**`config/frpc.toml`** (on your Mac):
```toml
serverAddr = "<hetzner-ip>"
auth.token = "<your-token>"
```

**`config/frps.toml`** (deployed to the VM):
```toml
auth.token = "<your-token>"
```

Both sides must use the same token or the tunnel won't connect.

---

## Step 5 — Deploy frps to the VM

```bash
scp config/frps.toml user@<hetzner-ip>:/etc/frps.toml
```

Run frps on the VM:

```bash
frps -c /etc/frps.toml
```

To keep it running across reboots, create a systemd service:

```bash
cat > /etc/systemd/system/frps.service <<EOF
[Unit]
Description=frp server
After=network.target

[Service]
ExecStart=/usr/local/bin/frps -c /etc/frps.toml
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now frps
```

---

## Step 6 — Deploy nginx + TLS

DNS must be live before running this (certbot validates via HTTP).

From your Mac:

```bash
./config/deploy-nginx.sh user@<hetzner-ip>
```

This copies `config/nginx-frontend.conf` and `config/nginx-stream.conf` to
the VM, symlinks them into `sites-enabled`, and runs certbot to obtain
Let's Encrypt certs for both domains.

---

## Step 7 — Switch the app to production mode

In `config/settings.toml`:

```toml
[streaming]
mode = "production"
```

This makes the FastAPI server embed `wss://stream-smash.felixscherz.me`
in the watch page instead of the local WS URL.

---

## Step 8 — Start everything and test

On your Mac:

```bash
# 1. Start OvenMediaEngine
docker start ome

# 2. Start the unified server
source .venv/bin/activate
python main.py

# 3. Start the frp tunnel (separate terminal)
frpc -c config/frpc.toml

# 4. Start OBS streaming
```

Then open `https://smash.felixscherz.me/lobby` from another machine to verify
the full path: lobby → start match → watch page → WebRTC stream.
