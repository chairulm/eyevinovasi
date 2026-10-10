#!/usr/bin/env bash
# CCTV Map — one-shot installer for a fresh Ubuntu 24.04 VM (idempotent: re-run to update)
#
#   sudo bash deploy/install.sh                       # HTTP on the VM's IP
#   sudo bash deploy/install.sh --auth                # + password-protect the site (prompts once;
#                                                     #   stays on for later re-runs, --no-auth removes)
#   sudo bash deploy/install.sh --domain cctv.example.com --email you@example.com   # + HTTPS
#
# Layout:  /opt/cctv-map (code, venv)   /opt/cctv-map/data (cameras, frames — kept on update)
# Services: cctv-map (uvicorn :8080, localhost only) behind nginx :80/:443
#           cctv-grabber (frame capture for analytics, installed but NOT enabled)
set -euo pipefail

APP_DIR=/opt/cctv-map
APP_USER=cctv
DB_NAME=cctvmap
# peer auth over the local socket: the "cctv" OS user logs in as DB role "cctv" — no password
DB_URL="postgresql:///$DB_NAME"
DOMAIN=""; EMAIL=""; AUTH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email)  EMAIL="$2";  shift 2 ;;
    --auth)   AUTH=1;      shift ;;
    --no-auth) AUTH=-1;    shift ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SRC_DIR/server/app.py" ]] || { echo "Run from inside the repo folder (deploy/install.sh)"; exit 1; }
log() { echo -e "\n\e[1;34m==>\e[0m $*"; }

# ------------------------------------------------------------------ packages
log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ffmpeg nginx ufw fail2ban apache2-utils postgresql \
  rsync curl unzip ca-certificates unattended-upgrades
timedatectl set-timezone Asia/Jakarta || true

# ------------------------------------------------------------------ app files
log "Deploying app to $APP_DIR"
id -u "$APP_USER" &>/dev/null || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
rsync -a --delete --exclude 'data/' --exclude '.venv/' --exclude '__pycache__/' --exclude '.git/' "$SRC_DIR"/ "$APP_DIR"/
rsync -a "$SRC_DIR/data/seed/" "$APP_DIR/data/seed/"          # refresh seed, keep live data
[[ -f "$APP_DIR/data/cameras.json" ]] || cp -a "$SRC_DIR"/data/cameras.* "$APP_DIR/data/" 2>/dev/null || true

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ------------------------------------------------------------------ database
log "PostgreSQL: role, database, migrations"
systemctl enable --now postgresql
sudo -u postgres psql -qtAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE ROLE $APP_USER LOGIN"
sudo -u postgres psql -qtAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE DATABASE $DB_NAME OWNER $APP_USER"
sudo -u "$APP_USER" env DATABASE_URL="$DB_URL" bash -c "cd $APP_DIR && .venv/bin/python -m db.migrate"

# ------------------------------------------------------------------ systemd
log "Installing systemd services"
cat > /etc/systemd/system/cctv-map.service <<EOF
[Unit]
Description=CCTV Map (FastAPI)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=REFRESH_HOURS=12
Environment=HEALTH_MINUTES=30
Environment=PYTHONUNBUFFERED=1
Environment=DATABASE_URL=$DB_URL
Environment=RESTREAM=1
ExecStart=$APP_DIR/.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8080 --proxy-headers --workers 1
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data $APP_DIR/web /run/postgresql

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/cctv-grabber.service <<EOF
[Unit]
Description=CCTV Map frame grabber (analytics)
After=cctv-map.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=RESTREAM=1
ExecStart=$APP_DIR/.venv/bin/python -m analytics.frame_grabber --interval 60 --workers 4
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------------------------------------------ re-stream (MediaMTX)
MTX_VERSION=v1.9.3
log "MediaMTX re-stream server $MTX_VERSION"
case "$(uname -m)" in
  x86_64) MTX_ARCH=amd64 ;;
  aarch64) MTX_ARCH=arm64v8 ;;
  *) echo "unsupported CPU $(uname -m) for MediaMTX"; exit 1 ;;
esac
if ! /usr/local/bin/mediamtx --version 2>/dev/null | grep -qx "$MTX_VERSION"; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/mtx.tgz" \
    "https://github.com/bluenviron/mediamtx/releases/download/$MTX_VERSION/mediamtx_${MTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
  tar xzf "$tmp/mtx.tgz" -C "$tmp" mediamtx
  install -m 755 "$tmp/mediamtx" /usr/local/bin/mediamtx
  rm -rf "$tmp"
fi
/usr/local/bin/mediamtx --version

cat > /etc/systemd/system/cctv-restream.service <<EOF
[Unit]
Description=CCTV Map re-stream (MediaMTX): pulls each camera once, on demand
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/data
# config is generated by the app (restream/config.py) and hot-reloaded by MediaMTX
ExecStart=/usr/local/bin/mediamtx $APP_DIR/data/mediamtx.yml
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------------------------------------------ nginx
log "Configuring nginx"
SERVER_NAME="${DOMAIN:-_}"
AUTH_BLOCK=""
# login stays on across re-runs once a password file exists (use --no-auth to turn it off)
[[ $AUTH -eq 0 && -f /etc/nginx/.cctv_htpasswd ]] && AUTH=1
if [[ $AUTH -eq 1 ]]; then
  if [[ ! -f /etc/nginx/.cctv_htpasswd ]]; then
    read -rp "Username for the site: " U
    htpasswd -c /etc/nginx/.cctv_htpasswd "$U"     # prompts for the password
  fi
  AUTH_BLOCK='auth_basic "CCTV Map"; auth_basic_user_file /etc/nginx/.cctv_htpasswd;'
fi

cat > /etc/nginx/conf.d/cctv-ratelimit.conf <<'EOF'
limit_req_zone $binary_remote_addr zone=cctv_api:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=cctv_hls:10m rate=40r/s;
EOF

cat > /etc/nginx/sites-available/cctv-map <<EOF
server {
    listen 80 default_server;
    server_name $SERVER_NAME;
    server_tokens off;
    client_max_body_size 1m;
    $AUTH_BLOCK

    gzip on;
    gzip_types application/json application/geo+json application/javascript text/css application/vnd.apple.mpegurl;

    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    location /live/ {
        limit_req zone=cctv_hls burst=80 nodelay;
        proxy_pass http://127.0.0.1:8888/;
        proxy_buffering off;
        proxy_read_timeout 60s;
    }
    location /hls {
        limit_req zone=cctv_hls burst=80 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_buffering off;
        proxy_read_timeout 60s;
    }
    location /api/ {
        limit_req zone=cctv_api burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 60s;
    }
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
EOF
ln -sf /etc/nginx/sites-available/cctv-map /etc/nginx/sites-enabled/cctv-map
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# ------------------------------------------------------------------ firewall
log "Firewall (ufw) + fail2ban"
# keep every port sshd is actually listening on, so we never lock you out
# (Ubuntu 24.04 may run ssh via socket activation, so check sshd -T, ssh.socket and live listeners)
SSH_PORTS=$( {
  echo 22
  sshd -T 2>/dev/null | awk '/^port /{print $2}'
  systemctl show ssh.socket -p Listen --value 2>/dev/null | grep -oE '[0-9]+ \(Stream\)' | awk '{print $1}'
  # public sshd listeners only (skip 127.0.0.1/::1, e.g. X11-forwarding port 6010)
  ss -tlnpH 2>/dev/null | awk '/sshd/ && $4 !~ /^(127\.|\[::1\]|::1)/ {n=split($4,a,":"); print a[n]}'
} | sort -un )
echo "  allowing SSH on: $SSH_PORTS"
for p in $SSH_PORTS; do ufw allow "$p/tcp" comment 'ssh' >/dev/null; done
# remove stale 'ssh' rules from earlier runs that are no longer real SSH ports (e.g. 6010)
for p in $(ufw status | awk '/# ssh/ {split($1,a,"/"); print a[1]}' | sort -un); do
  if ! grep -qx "$p" <<<"$SSH_PORTS"; then ufw --force delete allow "$p/tcp" >/dev/null && echo "  removed stale rule $p/tcp"; fi
done
ufw allow 80/tcp comment 'http' >/dev/null
ufw allow 443/tcp comment 'https' >/dev/null
ufw --force enable
systemctl enable --now fail2ban

# ------------------------------------------------------------------ first data load
log "Live scrape + stream health check (can take 3-8 min)"
systemctl stop cctv-map 2>/dev/null || true
sudo -u "$APP_USER" env DATABASE_URL="$DB_URL" RESTREAM=1 bash -c "cd $APP_DIR && timeout 1200 .venv/bin/python -m collector.run --health --progress" || \
  echo "!! live scrape had errors — app will use seed data; see output above"

sudo -u "$APP_USER" env DATABASE_URL="$DB_URL" RESTREAM=1 bash -c "cd $APP_DIR && .venv/bin/python -m restream.config"

systemctl daemon-reload
systemctl enable --now cctv-restream
systemctl restart cctv-restream
systemctl enable --now cctv-map
systemctl restart cctv-map

# ------------------------------------------------------------------ TLS
if [[ -n "$DOMAIN" && -n "$EMAIL" ]]; then
  log "HTTPS for $DOMAIN (DNS A record must already point to this VM)"
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --redirect -n || \
    echo "!! certbot failed — check DNS, then re-run this script"
fi

sleep 3
IP=$(hostname -I | awk '{print $1}')
log "Done"
curl -s http://127.0.0.1:8080/api/stats && echo
echo "Open:  http://${DOMAIN:-$IP}/"
echo "Logs:  journalctl -u cctv-map -f"
echo "Check: sudo bash $APP_DIR/deploy/check.sh"
echo "Analytics frame capture (optional): sudo systemctl enable --now cctv-grabber"
