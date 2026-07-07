#!/usr/bin/env bash
# Deploy NetBox + Nemotron + Cisco AI Defense demo to cisco-web-1-arm.
#
# Pattern: same as ~/epoch-dev/{aria,sage}/deploy.sh — pull secrets from OpenBao,
# rsync source, docker compose up, then tail logs.
#
# Usage:
#   ./deploy.sh                # full deploy (rsync + rebuild + up)
#   ./deploy.sh --dry-run      # show what would happen
#   ./deploy.sh --logs         # tail orchestrator logs
#   ./deploy.sh --seed         # one-shot seed NetBox
#   ./deploy.sh --down         # stop the stack
#   ./deploy.sh --rotate-secrets  # regenerate NetBox/DB/Redis secrets

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Config ----
REMOTE_HOST="129.80.113.130"
REMOTE_USER="ubuntu"
REMOTE_PATH="/srv/aidefense-demo"
SSH_KEY="${HOME}/.ssh/cisco_web_1_ed25519"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10"

DRY_RUN=false; LOGS=false; SEED=false; DOWN=false; ROTATE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run)         DRY_RUN=true ;;
    --logs)            LOGS=true ;;
    --seed)            SEED=true ;;
    --down)            DOWN=true ;;
    --rotate-secrets)  ROTATE=true ;;
    -h|--help)         sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

ssh_cmd() { ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_HOST" "$@"; }

# ---- OpenBao session ----
export BAO_ADDR="${BAO_ADDR:-https://vault.uppernyack.com}"
if ! bao token lookup >/dev/null 2>&1; then
  echo "OpenBao session not active. Logging in..."
  bao login -method=userpass username="${BAO_USER:-<your-username>}"
fi

# ---- Subcommands that don't need secrets ----
if $LOGS; then
  ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose logs -f --tail=200 orchestrator"
  exit 0
fi
if $DOWN; then
  ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose down"
  exit 0
fi
if $SEED; then
  echo "== Rsync seed/ to remote so we run latest seed code =="
  rsync -az -e "ssh $SSH_OPTS" --exclude '__pycache__' ./seed/ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/seed/"
  ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose --profile seed build seed && sudo docker compose --profile seed run --rm seed"
  exit 0
fi

# ---- Pull/generate secrets ----
bao_get() { bao kv get -field="$2" "$1" 2>/dev/null || true; }
bao_put_field() {
  local path="$1" field="$2" value="$3"
  local existing; existing=$(bao kv get -format=json "$path" 2>/dev/null || echo '{}')
  local merged
  merged=$(echo "$existing" | python3 -c "
import json,sys
d=json.load(sys.stdin).get('data',{}).get('data',{})
d['$field']='$value'
print(' '.join(f'{k}={json.dumps(v)}' for k,v in d.items()))
")
  eval "bao kv put $path $merged" >/dev/null
}

rand50() { openssl rand -base64 64 | tr -d '/+\n' | head -c 50; }
rand_token() { openssl rand -hex 20; }

ensure_secret() {
  local path="$1" field="$2" generator="$3"
  local v; v=$(bao_get "$path" "$field")
  if [ -z "$v" ] || $ROTATE; then
    v=$($generator)
    bao_put_field "$path" "$field" "$v"
    echo "  generated $path:$field" >&2   # diagnostic → stderr, don't pollute return value
  fi
  printf '%s' "$v"                       # no trailing newline
}

echo "== Resolving secrets via OpenBao =="

NIM_KEY=$(bao_get infra/api/nvidia-build-netbox-demo key)
AID_KEY=$(bao_get infra/api/cisco-ai-defense key)
[ -z "$NIM_KEY" ] && { echo "FAIL: infra/api/nvidia-build-netbox-demo not set"; exit 1; }
[ -z "$AID_KEY" ] && { echo "FAIL: infra/api/cisco-ai-defense not set"; exit 1; }

NETBOX_SECRET_KEY=$(ensure_secret infra/api/netbox-demo secret_key rand50)
NETBOX_SUPER_PW=$(ensure_secret infra/api/netbox-demo superuser_password rand50)
NETBOX_API_TOKEN=$(ensure_secret infra/api/netbox-demo api_token rand_token)
# Read-only token for netbox-mcp — created manually in NetBox with write_enabled=false.
# If the key isn't in OpenBao yet, the MCP falls back to the superuser token (less safe).
NETBOX_MCP_TOKEN_READONLY=$(bao_get infra/api/netbox-demo mcp_token_readonly)
[ -z "$NETBOX_MCP_TOKEN_READONLY" ] && NETBOX_MCP_TOKEN_READONLY="$NETBOX_API_TOKEN" && \
  echo "  ⚠ no RO token in vault — netbox-mcp will use superuser (run setup-mcp-readonly-token.sh)"
PG_PW=$(ensure_secret infra/db/netbox-demo-pg password rand50)
REDIS_PW=$(ensure_secret infra/db/netbox-demo-redis-queue password rand50)
REDIS_CACHE_PW=$(ensure_secret infra/db/netbox-demo-redis-cache password rand50)

# ---- Compose .env (lives only on remote /srv/aidefense-demo/compose/.env) ----
ENV_TMP=$(mktemp); trap "rm -f $ENV_TMP" EXIT
cat > "$ENV_TMP" <<EOF
DEMO_DOMAIN=aidefense-demo.uppernyack.com

AI_DEFENSE_API_KEY=$AID_KEY
AI_DEFENSE_REGION=us
AI_DEFENSE_MODE=api
AI_DEFENSE_GATEWAY_URL=
AI_DEFENSE_BASE_URL=
AI_DEFENSE_MODEL_LABEL=aidefense-demo
AI_DEFENSE_DEFAULT_RULES=

NIM_API_KEY=$NIM_KEY
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=nvidia/llama-3.3-nemotron-super-49b-v1

NETBOX_VERSION=v4.4-3.4.0
NETBOX_SECRET_KEY=$NETBOX_SECRET_KEY
NETBOX_SUPERUSER_NAME=admin
NETBOX_SUPERUSER_EMAIL=admin@aidefense-demo.local
NETBOX_SUPERUSER_PASSWORD=$NETBOX_SUPER_PW
NETBOX_API_TOKEN=$NETBOX_API_TOKEN
NETBOX_MCP_TOKEN_READONLY=$NETBOX_MCP_TOKEN_READONLY

POSTGRES_DB=netbox
POSTGRES_USER=netbox
POSTGRES_PASSWORD=$PG_PW

REDIS_PASSWORD=$REDIS_PW
REDIS_CACHE_PASSWORD=$REDIS_CACHE_PW

ORCHESTRATOR_LOG_LEVEL=INFO
GATE_INPUT_ENABLED=true
GATE_TOOL_ARGS_ENABLED=true
GATE_OUTPUT_ENABLED=true
EOF

if $DRY_RUN; then
  echo "== DRY RUN — would rsync to $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH and bring stack up =="
  echo "== .env contents (sensitive values masked) =="
  sed 's/=.*$/=***/' "$ENV_TMP"
  exit 0
fi

# ---- Ensure remote dir exists ----
ssh_cmd "sudo mkdir -p $REMOTE_PATH && sudo chown $REMOTE_USER:$REMOTE_USER $REMOTE_PATH"

# ---- Rsync (exclude local .env / .git / volumes) ----
echo "== Rsync to $REMOTE_HOST:$REMOTE_PATH =="
rsync -az --delete \
  --exclude '.git' --exclude '.gitignore' --exclude '.env' --exclude '.env.local' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' --exclude 'venv' \
  --exclude 'caddy_data' --exclude 'postgres_data' --exclude 'redis_data' \
  -e "ssh $SSH_OPTS" ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"

# ---- Push .env separately, chmod 600 ----
scp $SSH_OPTS "$ENV_TMP" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/compose/.env"
ssh_cmd "chmod 600 $REMOTE_PATH/compose/.env"

# ---- Build + up ----
echo "== docker compose build + up -d =="
ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose build && sudo docker compose up -d"

# Caddy uses a bind-mounted Caddyfile that loses its inode when rsync replaces the file.
# Force-restart Caddy so the new config is actually loaded.
echo "== forcing caddy restart so the bind-mounted Caddyfile reload sticks =="
ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose restart caddy"

# ---- Wait for NetBox healthcheck then seed if needed ----
echo "== Waiting for NetBox to become healthy (this can take 60-120s on first run) =="
for i in {1..40}; do
  STATUS=$(ssh_cmd "sudo docker inspect --format='{{.State.Health.Status}}' aidefense-demo-netbox-1 2>/dev/null || echo unknown")
  echo "  [$i/40] netbox health: $STATUS"
  if [ "$STATUS" = "healthy" ]; then break; fi
  sleep 5
done

echo ""
echo "== Stack status =="
ssh_cmd "cd $REMOTE_PATH/compose && sudo docker compose ps"

echo ""
echo "== Done =="
echo "Live URL:        https://aidefense-demo.uppernyack.com"
echo "NetBox internal: behind reverse proxy (orchestrator drives all NetBox calls)"
echo "Logs:            ./deploy.sh --logs"
echo "Seed data:       ./deploy.sh --seed"
