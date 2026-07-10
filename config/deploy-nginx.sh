#!/usr/bin/env bash
# Usage: ./config/deploy-nginx.sh user@your-hetzner-ip
# Copies nginx configs, enables sites, and obtains Let's Encrypt certs.

set -euo pipefail

SERVER="${1:?Usage: $0 user@hetzner-ip}"

echo "==> Copying nginx configs to $SERVER..."
scp config/nginx-frontend.conf "$SERVER":/etc/nginx/sites-available/smash.felixscherz.me
scp config/nginx-stream.conf   "$SERVER":/etc/nginx/sites-available/stream-smash.felixscherz.me

echo "==> Enabling sites and obtaining certs..."
ssh "$SERVER" bash <<'EOF'
set -euo pipefail

# Enable sites
ln -sf /etc/nginx/sites-available/smash.felixscherz.me \
       /etc/nginx/sites-enabled/smash.felixscherz.me
ln -sf /etc/nginx/sites-available/stream-smash.felixscherz.me \
       /etc/nginx/sites-enabled/stream-smash.felixscherz.me

# Test nginx config before reloading
nginx -t

# Obtain certs (certbot must be installed: apt install certbot python3-certbot-nginx)
certbot --nginx -d smash.felixscherz.me --non-interactive --agree-tos -m felixw.scherz@gmail.com
certbot --nginx -d stream-smash.felixscherz.me --non-interactive --agree-tos -m felixw.scherz@gmail.com

# Reload nginx with new certs
systemctl reload nginx
echo "Done! Both domains are live with TLS."
EOF
