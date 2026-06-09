#!/usr/bin/env bash
set -Eeuo pipefail

export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

ARCHIVE="${1:-/tmp/competegraph.tar.gz}"
APP_ROOT="${2:-/mnt/competegraph}"
BACKEND_PORT="${3:-18000}"
FRONTEND_PORT="${4:-18080}"
APP_DIR="$APP_ROOT/app"
SERVICE_NAME="competegraph-api"
NGINX_CONF="/etc/nginx/conf.d/competegraph.conf"
PYTHON_VERSION="3.11"

log() {
  printf '\n>>> %s\n' "$*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must run as root." >&2
    exit 1
  fi
}

install_packages() {
  log "Installing system packages"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates nginx python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y curl ca-certificates nginx python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y curl ca-certificates nginx python3
  else
    echo "Unsupported Linux distribution: apt-get/dnf/yum not found." >&2
    exit 1
  fi
}

ensure_uv() {
  log "Checking uv"
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="/root/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installation failed or uv is not on PATH." >&2
    exit 1
  fi
}

deploy_files() {
  log "Deploying files to $APP_DIR"
  mkdir -p "$APP_DIR"
  if [ -f "$APP_DIR/backend/.env" ]; then
    cp "$APP_DIR/backend/.env" /tmp/competegraph_backend_env.keep
  fi
  tar -xzf "$ARCHIVE" -C "$APP_DIR"
  if [ -f /tmp/competegraph_backend_env.keep ]; then
    mv /tmp/competegraph_backend_env.keep "$APP_DIR/backend/.env"
  fi
  mkdir -p "$APP_DIR/data"
}

install_backend() {
  log "Installing backend dependencies"
  export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  cd "$APP_DIR/backend"
  rm -rf .venv
  uv python install "$PYTHON_VERSION"
  uv sync --python "$PYTHON_VERSION" --index-url "$UV_DEFAULT_INDEX"
}

write_systemd_service() {
  log "Writing systemd service"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=CompeteGraph FastAPI backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}/backend
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=UV_PYTHON=${PYTHON_VERSION}
ExecStart=/usr/bin/env uv run --python ${PYTHON_VERSION} uvicorn cg.main:app --host 127.0.0.1 --port ${BACKEND_PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
}

write_nginx_config() {
  log "Writing nginx config"
  mkdir -p /etc/nginx/conf.d

  if [ "$FRONTEND_PORT" = "80" ]; then
    for default_conf in /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf; do
      if [ -e "$default_conf" ]; then
        mv "$default_conf" "${default_conf}.disabled-by-competegraph-$(date +%s)"
      fi
    done
  fi

  cat > "$NGINX_CONF" <<EOF
server {
    listen ${FRONTEND_PORT} default_server;
    server_name _;

    client_max_body_size 50m;
    root ${APP_DIR}/frontend/dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/api/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    location /files/ {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/files/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /health {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/health;
        proxy_set_header Host \$host;
    }

    location = /openapi.json {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/openapi.json;
        proxy_set_header Host \$host;
    }

    location /docs {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/docs;
        proxy_set_header Host \$host;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:${BACKEND_PORT}/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF

  nginx -t
  systemctl enable nginx
  systemctl restart nginx
}

open_local_firewall() {
  log "Opening local firewall for HTTP when available"
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=http || true
    firewall-cmd --reload || true
  fi

  if command -v ufw >/dev/null 2>&1; then
    ufw allow ${FRONTEND_PORT}/tcp || true
  fi
}

verify() {
  log "Verifying services"
  systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,18p'
  for attempt in 1 2 3 4 5; do
    if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" >/tmp/competegraph-health.json; then
      cat /tmp/competegraph-health.json
      printf '\n'
      break
    fi
    if [ "$attempt" = "5" ]; then
      curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health"
    fi
    sleep 1
  done
  curl -fsSI "http://127.0.0.1:${FRONTEND_PORT}/" | sed -n '1,8p'
}

require_root
install_packages
ensure_uv
deploy_files
install_backend
write_systemd_service
write_nginx_config
open_local_firewall
verify

log "CompeteGraph is deployed at $APP_ROOT"
