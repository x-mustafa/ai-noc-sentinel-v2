#!/usr/bin/env bash
# =============================================================================
# NOC Sentinel v2 — One-Shot Production Deployment Script
# =============================================================================
# Run as root (or with sudo) on a fresh Ubuntu 22.04 / Debian 12 server.
#
# What this script does:
#   1. Creates system user 'nocsentinel'
#   2. Installs system dependencies (python3, pip, nodejs, npm, nginx, mysql)
#   3. Clones the repo to /opt/noc-sentinel
#   4. Creates a Python virtualenv and installs pip requirements
#   5. Installs WhatsApp bridge dependencies (npm)
#   6. Copies nginx config and enables the site
#   7. Installs and enables the systemd service
#   8. Installs PM2 globally and starts the WhatsApp bridge
#   9. Prints a post-install checklist
#
# Usage:
#   sudo bash deploy/install.sh
#
# Customise these variables before running:
# =============================================================================

DOMAIN="noc.example.com"                         # ← your domain / IP
APP_DIR="/opt/noc-sentinel"
APP_USER="nocsentinel"
REPO_URL="https://github.com/x-mustafa/ai-noc-sentinel-v2.git"
PYTHON_BIN="python3"
LOG_DIR="/var/log/noc-sentinel"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# =============================================================================
# 0. Preflight
# =============================================================================
[[ $EUID -ne 0 ]] && error "Run this script as root: sudo bash deploy/install.sh"
info "Starting NOC Sentinel deployment on $(hostname) — $(date)"

# =============================================================================
# 1. System user
# =============================================================================
info "Creating system user '$APP_USER'..."
id "$APP_USER" &>/dev/null || useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" "$APP_USER"

# =============================================================================
# 2. System dependencies
# =============================================================================
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nodejs npm \
    nginx \
    git curl unzip \
    mysql-client \
    libssl-dev libffi-dev \
    logrotate

# =============================================================================
# 3. Clone / update repo
# =============================================================================
if [[ -d "$APP_DIR/.git" ]]; then
    info "Repo already exists — pulling latest..."
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
else
    info "Cloning repo to $APP_DIR..."
    git clone "$REPO_URL" "$APP_DIR"
    chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
fi

# =============================================================================
# 4. Python virtualenv + dependencies
# =============================================================================
info "Setting up Python virtualenv..."
sudo -u "$APP_USER" $PYTHON_BIN -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
info "Python dependencies installed."

# =============================================================================
# 5. WhatsApp bridge (Node.js)
# =============================================================================
if [[ -f "$APP_DIR/whatsapp/package.json" ]]; then
    info "Installing WhatsApp bridge npm dependencies..."
    sudo -u "$APP_USER" npm --prefix "$APP_DIR/whatsapp" install --production -q
fi

# =============================================================================
# 6. Log directory
# =============================================================================
info "Creating log directory $LOG_DIR..."
mkdir -p "$LOG_DIR"
chown "$APP_USER":"$APP_USER" "$LOG_DIR"

# =============================================================================
# 7. Nginx configuration
# =============================================================================
info "Configuring nginx..."
NGINX_CONF="/etc/nginx/sites-available/noc-sentinel"
PROXY_PARAMS="/etc/nginx/noc-proxy-params.conf"

cp "$APP_DIR/deploy/nginx.conf"           "$NGINX_CONF"
cp "$APP_DIR/deploy/noc-proxy-params.conf" "$PROXY_PARAMS"

# Substitute domain placeholder
sed -i "s/noc\.example\.com/$DOMAIN/g" "$NGINX_CONF"

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/noc-sentinel
rm -f /etc/nginx/sites-enabled/default     # remove default nginx page

nginx -t && systemctl reload nginx
info "Nginx configured for domain: $DOMAIN"

# =============================================================================
# 8. Systemd service
# =============================================================================
info "Installing systemd service..."
cp "$APP_DIR/deploy/noc-sentinel.service" /etc/systemd/system/noc-sentinel.service
cp "$APP_DIR/deploy/noc-sentinel-worker.service" /etc/systemd/system/noc-sentinel-worker.service

# Point WorkingDirectory to our install path
sed -i "s|/opt/noc-sentinel|$APP_DIR|g" /etc/systemd/system/noc-sentinel.service
sed -i "s|/opt/noc-sentinel|$APP_DIR|g" /etc/systemd/system/noc-sentinel-worker.service
sed -i "s|User=nocsentinel|User=$APP_USER|g"    /etc/systemd/system/noc-sentinel.service
sed -i "s|Group=nocsentinel|Group=$APP_USER|g"  /etc/systemd/system/noc-sentinel.service
sed -i "s|User=nocsentinel|User=$APP_USER|g"    /etc/systemd/system/noc-sentinel-worker.service
sed -i "s|Group=nocsentinel|Group=$APP_USER|g"  /etc/systemd/system/noc-sentinel-worker.service

systemctl daemon-reload
systemctl enable noc-sentinel
systemctl enable noc-sentinel-worker
info "Systemd services installed. Start them after .env is configured."

# =============================================================================
# 9. PM2 for WhatsApp bridge
# =============================================================================
info "Installing PM2..."
npm install -g pm2 -q

PM2_CONF="$APP_DIR/deploy/ecosystem.config.js"
sed -i "s|/opt/noc-sentinel|$APP_DIR|g" "$PM2_CONF"

# Start PM2 as the app user
sudo -u "$APP_USER" pm2 start "$PM2_CONF"
sudo -u "$APP_USER" pm2 save
# Register PM2 startup (prints a command to run — do it manually)
PM2_STARTUP=$(sudo -u "$APP_USER" pm2 startup systemd -u "$APP_USER" --hp "$APP_DIR" 2>&1 | grep "sudo env")
warn "Run this command to enable PM2 on boot:"
echo "  $PM2_STARTUP"

# =============================================================================
# 10. Log rotation
# =============================================================================
cat > /etc/logrotate.d/noc-sentinel <<EOF
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $APP_USER $APP_USER
}
EOF
info "Log rotation configured."

# =============================================================================
# Post-install checklist
# =============================================================================
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  NOC Sentinel deployment complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo -e "${YELLOW}REQUIRED — complete these steps before starting the service:${NC}"
echo ""
echo "  1. Copy and fill .env:"
echo "       cp $APP_DIR/.env.example $APP_DIR/.env"
echo "       nano $APP_DIR/.env"
echo "     (set DB credentials, AI keys, APP_SECRET, etc.)"
echo ""
echo "  2. Create the MySQL database and user:"
echo "       mysql -u root -p <<SQL"
echo "       CREATE DATABASE noc_sentinel CHARACTER SET utf8mb4;"
echo "       CREATE USER 'noc'@'localhost' IDENTIFIED BY 'CHANGE_ME';"
echo "       GRANT SELECT,INSERT,UPDATE,DELETE ON noc_sentinel.* TO 'noc'@'localhost';"
echo "       FLUSH PRIVILEGES;"
echo "       SQL"
echo ""
echo "  3. Install SSL certificate (Let's Encrypt recommended):"
echo "       apt install certbot python3-certbot-nginx -y"
echo "       certbot --nginx -d $DOMAIN"
echo ""
echo "  4. Generate a license for this machine (if license enforcement is on):"
echo "       cd $APP_DIR && python tools/generate_license.py"
echo ""
echo "  5. Apply database migrations:"
echo "       cd $APP_DIR && $APP_DIR/venv/bin/python tools/migrate.py"
echo ""
echo "  6. Create your first local admin (or configure LDAP first):"
echo "       cd $APP_DIR && $APP_DIR/venv/bin/python tools/create_admin.py --username admin --password CHANGE_ME_NOW"
echo ""
echo "  7. Start the FastAPI and worker services:"
echo "       systemctl start noc-sentinel"
echo "       systemctl start noc-sentinel-worker"
echo "       systemctl status noc-sentinel"
echo "       systemctl status noc-sentinel-worker"
echo ""
echo "  8. Verify health:"
echo "       curl https://$DOMAIN/api/health"
echo ""
echo "  9. Scan WhatsApp QR code (if using WA integration):"
echo "       pm2 logs noc-whatsapp"
echo ""
echo -e "${GREEN}Done. Good luck!${NC}"
