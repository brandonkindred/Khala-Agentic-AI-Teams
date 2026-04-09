#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy-droplet.sh
#
# One-shot deployment script for the Strands Agents Docker Compose stack on a
# fresh Ubuntu 22.04 / 24.04 DigitalOcean droplet (or any Debian/Ubuntu VM).
#
# Usage (as root on the droplet):
#
#   # Interactive (prompts for secrets):
#   curl -fsSL https://raw.githubusercontent.com/deepthought42/strands-agents/main/docker/deploy-droplet.sh | bash
#
#   # Or after cloning the repo:
#   sudo bash docker/deploy-droplet.sh
#
#   # Non-interactive (pass secrets via env):
#   OLLAMA_API_KEY=sk-... POSTGRES_PASSWORD=change-me sudo -E bash docker/deploy-droplet.sh
#
# The script is idempotent: re-running it will skip steps that are already
# done (swap, Docker install, repo clone, .env) and re-apply the stack.
#
# Recommended droplet: Basic 16 GB / 8 vCPU or larger. Smaller droplets will
# OOM during the initial parallel build of ~22 Python images.
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/deepthought42/strands-agents.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/strands-agents}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-8}"
COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-4}"
UNIFIED_API_WORKERS="${UNIFIED_API_WORKERS:-2}"

# Ports to open in the host firewall.
# 4201 = Angular UI, 8888 = Unified API direct (handy for debugging).
# Postgres (5432), Temporal gRPC (7233), Temporal UI (8080) stay closed —
# reach them via SSH tunnel instead.
OPEN_PORTS=("4201/tcp" "8888/tcp")

# ── Helpers ────────────────────────────────────────────────────────────────
log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root (use: sudo bash $0)"
  fi
}

prompt_if_missing() {
  # $1 = variable name, $2 = human description, $3 = "secret" or ""
  local var="$1" desc="$2" secret="${3:-}"
  if [[ -z "${!var:-}" ]]; then
    if [[ -t 0 ]]; then
      if [[ "$secret" == "secret" ]]; then
        read -r -s -p "Enter ${desc}: " value; echo
      else
        read -r -p "Enter ${desc}: " value
      fi
      printf -v "$var" '%s' "$value"
      export "$var"
    else
      die "${var} is not set and stdin is not a TTY. Re-run with: ${var}=... bash $0"
    fi
  fi
}

# ── Step 1: sanity checks ──────────────────────────────────────────────────
step_preflight() {
  log "Step 1/10: Preflight checks"
  require_root

  if ! grep -qiE 'ubuntu|debian' /etc/os-release; then
    warn "This script is tested on Ubuntu/Debian. Proceeding anyway."
  fi

  local mem_mb
  mem_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
  log "  RAM: ${mem_mb} MB"
  if (( mem_mb < 7500 )); then
    warn "  Droplet has < 8 GB RAM. The build will likely OOM."
    warn "  Recommended: Basic 16 GB droplet. Continuing in 5s..."
    sleep 5
  fi

  local disk_gb
  disk_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
  log "  Free disk on /: ${disk_gb} GB"
  if (( disk_gb < 40 )); then
    warn "  Less than 40 GB free. Images + volumes may fill the disk."
  fi
}

# ── Step 2: secrets ────────────────────────────────────────────────────────
step_collect_secrets() {
  log "Step 2/10: Collecting secrets"
  prompt_if_missing OLLAMA_API_KEY "Ollama Cloud API key (from https://ollama.com/settings/keys)" secret
  : "${POSTGRES_PASSWORD:=}"
  if [[ -z "${POSTGRES_PASSWORD}" ]]; then
    POSTGRES_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    export POSTGRES_PASSWORD
    log "  Generated random POSTGRES_PASSWORD (saved to docker/.env)"
  fi
}

# ── Step 3: apt update + base packages ────────────────────────────────────
step_base_packages() {
  log "Step 3/10: Installing base packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    git curl ca-certificates gnupg lsb-release ufw jq
}

# ── Step 4: swap ──────────────────────────────────────────────────────────
step_swap() {
  log "Step 4/10: Ensuring ${SWAP_SIZE_GB} GB swap"
  if swapon --show | grep -q '/swapfile'; then
    log "  /swapfile already active, skipping"
  else
    fallocate -l "${SWAP_SIZE_GB}G" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    if ! grep -q '^/swapfile' /etc/fstab; then
      echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    log "  Swap enabled"
  fi
  sysctl -q vm.swappiness=10
  if ! grep -q '^vm.swappiness' /etc/sysctl.conf; then
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
  fi
}

# ── Step 5: Docker Engine ──────────────────────────────────────────────────
step_docker() {
  log "Step 5/10: Installing Docker Engine + Compose plugin"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "  Docker + compose plugin already installed: $(docker --version)"
    return
  fi

  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi

  local codename
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  systemctl enable --now docker
  log "  $(docker --version)"
  log "  $(docker compose version)"
}

# ── Step 6: firewall ──────────────────────────────────────────────────────
step_firewall() {
  log "Step 6/10: Configuring UFW"
  ufw allow OpenSSH >/dev/null
  for p in "${OPEN_PORTS[@]}"; do
    ufw allow "$p" >/dev/null
    log "  allowed $p"
  done
  if ! ufw status | grep -q "Status: active"; then
    ufw --force enable >/dev/null
    log "  UFW enabled"
  fi
}

# ── Step 7: clone repo ────────────────────────────────────────────────────
step_clone() {
  log "Step 7/10: Cloning repo to ${INSTALL_DIR}"
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "  Repo exists, fetching latest on ${REPO_BRANCH}"
    git -C "${INSTALL_DIR}" fetch --depth 1 origin "${REPO_BRANCH}"
    git -C "${INSTALL_DIR}" checkout "${REPO_BRANCH}"
    git -C "${INSTALL_DIR}" reset --hard "origin/${REPO_BRANCH}"
  else
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    git clone --depth 1 --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
  fi
}

# ── Step 8: .env + tuning ─────────────────────────────────────────────────
step_configure() {
  log "Step 8/10: Writing docker/.env and tuning"
  local env_file="${INSTALL_DIR}/docker/.env"
  local example="${INSTALL_DIR}/docker/.env.example"

  if [[ ! -f "${example}" ]]; then
    die "Missing ${example} — repo structure unexpected"
  fi

  cp "${example}" "${env_file}"
  # Inject secrets. Use | as sed delimiter because the key may contain /.
  sed -i "s|^OLLAMA_API_KEY=.*|OLLAMA_API_KEY=${OLLAMA_API_KEY}|" "${env_file}"
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD}|" "${env_file}"
  chmod 600 "${env_file}"
  log "  Wrote ${env_file} (0600)"

  # Reduce unified API workers to save RAM on small boxes.
  local dockerfile="${INSTALL_DIR}/backend/Dockerfile"
  if grep -q '"--workers", "4"' "${dockerfile}"; then
    sed -i "s|\"--workers\", \"4\"|\"--workers\", \"${UNIFIED_API_WORKERS}\"|" "${dockerfile}"
    log "  Tuned unified API workers: 4 → ${UNIFIED_API_WORKERS}"
  fi
}

# ── Step 9: network + build + up ──────────────────────────────────────────
step_compose_up() {
  log "Step 9/10: Creating network and bringing the stack up"
  cd "${INSTALL_DIR}"

  bash ./docker/ensure-network.sh

  log "  Building images (this takes 15–30 min on first run)"
  COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT}" \
    docker compose -f docker/docker-compose.yml --env-file docker/.env build

  log "  Starting containers (detached)"
  docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
}

# ── Step 10: verify ───────────────────────────────────────────────────────
step_verify() {
  log "Step 10/10: Waiting for the unified API to become healthy"
  cd "${INSTALL_DIR}"

  local deadline=$(( SECONDS + 300 ))
  while (( SECONDS < deadline )); do
    if curl -fsS http://localhost:8888/health >/dev/null 2>&1; then
      log "  Unified API is healthy"
      break
    fi
    sleep 5
  done

  if ! curl -fsS http://localhost:8888/health >/dev/null 2>&1; then
    warn "  Unified API did not report healthy within 5 minutes."
    warn "  Check: docker compose -f ${INSTALL_DIR}/docker/docker-compose.yml ps"
    warn "         docker compose -f ${INSTALL_DIR}/docker/docker-compose.yml logs --tail=200 strands-agents"
  fi

  echo
  docker compose -f docker/docker-compose.yml ps
  echo

  local public_ip
  public_ip="$(curl -fsS -m 3 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"

  log "Done. Access points:"
  echo "  Angular UI   : http://${public_ip}:4201"
  echo "  Unified API  : http://${public_ip}:8888/health"
  echo "  Temporal UI  : ssh -L 8080:localhost:8080 root@${public_ip}  # then open http://localhost:8080"
  echo
  log "Day-2 operations:"
  echo "  cd ${INSTALL_DIR}"
  echo "  docker compose -f docker/docker-compose.yml logs -f --tail=100 <service>"
  echo "  docker compose -f docker/docker-compose.yml restart <service>"
  echo "  docker compose -f docker/docker-compose.yml down          # stop (keeps data)"
  echo "  docker compose -f docker/docker-compose.yml down -v       # stop + WIPE volumes"
}

# ── Main ───────────────────────────────────────────────────────────────────
main() {
  log "Strands Agents — DigitalOcean droplet deploy"
  step_preflight
  step_collect_secrets
  step_base_packages
  step_swap
  step_docker
  step_firewall
  step_clone
  step_configure
  step_compose_up
  step_verify
}

main "$@"
