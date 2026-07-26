#!/usr/bin/env bash
# install.sh — deterministic bootstrap for the NUC AI appliance
#
# Usage (from scratch — one-liner):
#   git clone https://github.com/hbuddenberg/llama-cpp && \
#     GH_USER=hbuddenberg bash llama-cpp/nuc-infra/scripts/install.sh
#
# Or via curl (no prior clone needed):
#   curl -fsSL https://raw.githubusercontent.com/hbuddenberg/llama-cpp/main/nuc-infra/scripts/install.sh | \
#     GH_USER=hbuddenberg bash
#
# If WRAPPER_API_KEY is not set, a key is generated automatically in the format:
#   sk_llama-cpp_<uuid>
# The generated key is printed at the end — save it.
#
# Required env vars:
#   GH_USER          — GitHub username that owns the GHCR images
#
# Optional env vars:
#   WRAPPER_API_KEY  — Bearer token for ai-wrapper; auto-generated if not set
#   INSTALL_DIR      — where to clone/find the monorepo (default: $HOME/llama-cpp)
#   MODELS_HOST_DIR  — absolute path to model storage (default: $INSTALL_DIR/nuc-infra/data/models)
#   SKIP_MODELS      — set to "1" to skip huggingface model download
#   SKIP_TAILSCALE   — set to "1" to skip tailscale expose step
#   ENGINE_SOURCE_REF— git ref for engine builds (default: main)

set -euo pipefail
IFS=$'\n\t'

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[install]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── required env vars ────────────────────────────────────────────────────────
: "${GH_USER:?GH_USER is required (GitHub username owning the GHCR images)}"

# Auto-generate WRAPPER_API_KEY if not provided
_KEY_GENERATED=0
if [[ -z "${WRAPPER_API_KEY:-}" || "${WRAPPER_API_KEY:-}" == "change-me" ]]; then
    # Use /proc/sys/kernel/random/uuid when available (Linux); fall back to uuidgen
    _UUID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen 2>/dev/null || \
            od -x /dev/urandom | head -1 | awk '{print $2$3"-"$4"-"$5"-"$6"-"$7$8$9}')
    WRAPPER_API_KEY="sk_llama-cpp_${_UUID}"
    _KEY_GENERATED=1
    warn "WRAPPER_API_KEY not set — generated: ${WRAPPER_API_KEY}"
fi

# ── defaults ─────────────────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-$HOME/llama-cpp}"
INFRA_DIR="${INSTALL_DIR}/nuc-infra"
MODELS_HOST_DIR="${MODELS_HOST_DIR:-${INFRA_DIR}/data/models}"
SKIP_MODELS="${SKIP_MODELS:-0}"
SKIP_TAILSCALE="${SKIP_TAILSCALE:-0}"
ENGINE_SOURCE_REF="${ENGINE_SOURCE_REF:-main}"
INFRA_REPO="https://github.com/${GH_USER}/llama-cpp.git"

# ── tool version requirements ────────────────────────────────────────────────
PODMAN_MIN="4.0"
COMPOSE_MIN="1.0"

# ── helpers ──────────────────────────────────────────────────────────────────
version_ge() {
    # true if $1 >= $2 (simple dot-split comparison)
    local a b
    IFS='.' read -ra a <<< "${1%%[-+]*}"
    IFS='.' read -ra b <<< "${2%%[-+]*}"
    for i in "${!b[@]}"; do
        local av="${a[$i]:-0}" bv="${b[$i]:-0}"
        (( av > bv )) && return 0
        (( av < bv )) && return 1
    done
    return 0
}

require_cmd() {
    command -v "$1" &>/dev/null || die "'$1' not found — install it and re-run"
}

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        echo "${ID:-unknown}"
    else
        echo "unknown"
    fi
}

install_podman() {
    local os; os=$(detect_os)
    case "$os" in
        fedora|coreos|rhel|centos)
            info "Installing podman via dnf..."
            sudo dnf install -y podman podman-compose curl
            ;;
        ubuntu|debian|linuxmint)
            info "Installing podman via apt..."
            sudo apt-get update -qq
            sudo apt-get install -y podman podman-compose curl
            ;;
        arch)
            info "Installing podman via pacman..."
            sudo pacman -Sy --noconfirm podman podman-compose curl
            ;;
        *)
            die "Unsupported OS '$os'. Install podman >= $PODMAN_MIN and podman-compose >= $COMPOSE_MIN manually."
            ;;
    esac
}

# ── step 1: check / install dependencies ─────────────────────────────────────
info "Checking dependencies..."

if ! command -v podman &>/dev/null; then
    warn "podman not found — attempting install"
    install_podman
fi

PODMAN_VER=$(podman --version | grep -oP '[\d.]+' | head -1)
version_ge "$PODMAN_VER" "$PODMAN_MIN" || die "podman $PODMAN_VER is too old (need >= $PODMAN_MIN)"
ok "podman $PODMAN_VER"

if ! command -v podman-compose &>/dev/null; then
    warn "podman-compose not found — attempting install"
    install_podman
fi
ok "podman-compose $(podman-compose --version 2>&1 | grep -oP '[\d.]+' | head -1)"

if [[ "$SKIP_MODELS" != "1" ]]; then
    if ! command -v huggingface-cli &>/dev/null; then
        info "Installing huggingface_hub for model download..."
        if command -v pip3 &>/dev/null; then
            pip3 install --quiet --user huggingface_hub[cli]
            export PATH="$HOME/.local/bin:$PATH"
        elif command -v pipx &>/dev/null; then
            pipx install huggingface_hub[cli]
        else
            die "pip3 or pipx required to install huggingface-cli (or set SKIP_MODELS=1)"
        fi
    fi
    ok "huggingface-cli $(huggingface-cli --version 2>/dev/null || echo 'ok')"
fi

# ── step 2: clone or update monorepo ─────────────────────────────────────────
info "Setting up llama-cpp monorepo at $INSTALL_DIR..."

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repo already cloned — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone --depth 1 "$INFRA_REPO" "$INSTALL_DIR"
fi
ok "monorepo at $INSTALL_DIR"

# ── step 3: generate .env ────────────────────────────────────────────────────
ENV_FILE="$INFRA_DIR/.env"
info "Configuring $ENV_FILE..."

if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists — skipping generation (delete it to regenerate)"
else
    # Generate LibreChat secrets deterministically from the wrapper key + machine-id
    # (avoids shipping openssl rand to the user while still being reproducible per host)
    MACHINE_ID=$(cat /etc/machine-id 2>/dev/null || hostname)
    derive() {
        echo -n "${WRAPPER_API_KEY}:${MACHINE_ID}:${1}" | sha256sum | cut -c1-$2
    }

    cat > "$ENV_FILE" <<EOF
# Generated by install.sh — do not commit this file
GH_USER=${GH_USER}
WRAPPER_API_KEY=${WRAPPER_API_KEY}
MODELS_HOST_DIR=${MODELS_HOST_DIR}
ENGINE_SOURCE_REF=${ENGINE_SOURCE_REF}

# LibreChat secrets — derived from WRAPPER_API_KEY + machine-id
# To rotate: delete this file and re-run install.sh with the new key
CREDS_KEY=$(derive creds_key 64)
CREDS_IV=$(derive creds_iv 32)
JWT_SECRET=$(derive jwt_secret 64)
JWT_REFRESH_SECRET=$(derive jwt_refresh 64)
EOF
    chmod 600 "$ENV_FILE"
    ok ".env generated"
fi

# ── step 4: create data directories ──────────────────────────────────────────
info "Creating data directories..."
mkdir -p \
    "$INFRA_DIR/data/mongodb" \
    "$INFRA_DIR/data/qdrant" \
    "$MODELS_HOST_DIR"
ok "data dirs ready"

# ── step 5: pull GHCR images ─────────────────────────────────────────────────
info "Pulling GHCR images..."

IMAGES=(
    "ghcr.io/${GH_USER}/ai-wrapper:latest"
    "ghcr.io/${GH_USER}/llama-atomic:latest"
    "ghcr.io/${GH_USER}/llama-tom:latest"
)

for img in "${IMAGES[@]}"; do
    info "  pulling $img"
    if podman pull "$img" 2>&1; then
        ok "  $img"
    else
        warn "  $img — FAILED (CI may still be building; re-run when ready)"
    fi
done

# ── step 6: validate compose ──────────────────────────────────────────────────
info "Validating podman-compose config..."
podman-compose -f "$INFRA_DIR/podman-compose.yml" --env-file "$ENV_FILE" config > /dev/null
ok "compose config valid"

# ── step 7: start services ────────────────────────────────────────────────────
info "Starting services..."
podman-compose -f "$INFRA_DIR/podman-compose.yml" --env-file "$ENV_FILE" up -d
ok "services started"

# ── step 8: download models (optional) ───────────────────────────────────────
if [[ "$SKIP_MODELS" == "1" ]]; then
    warn "SKIP_MODELS=1 — skipping model download (run nuc-infra/scripts/fetch-models.sh manually)"
else
    info "Downloading models (this will take a while)..."
    MODELS_HOST_DIR="$MODELS_HOST_DIR" bash "$INFRA_DIR/scripts/fetch-models.sh"
    ok "models downloaded"
fi

# ── step 9: expose via tailscale (optional) ───────────────────────────────────
if [[ "$SKIP_TAILSCALE" == "1" ]]; then
    warn "SKIP_TAILSCALE=1 — skipping tailscale expose"
elif ! command -v tailscale &>/dev/null; then
    warn "tailscale not found — skipping expose step (install tailscale and run nuc-infra/scripts/tailscale-expose.sh manually)"
else
    info "Exposing port 5120 on tailnet..."
    bash "$INFRA_DIR/scripts/tailscale-expose.sh"
    ok "tailscale expose done"
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  NUC AI appliance installed successfully${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  LibreChat:  http://localhost:3000"
echo "  ai-wrapper: http://localhost:5120/v1"
echo "  Tailnet:    http://\$(tailscale ip -4 2>/dev/null || echo '<tailscale-ip>'):5120/v1"
echo ""
echo "  To check service status:"
echo "    podman-compose -f $INFRA_DIR/podman-compose.yml ps"
echo ""
echo "  To add a model:"
echo "    mkdir \$MODELS_HOST_DIR/<model-folder>"
echo "    # add model.gguf + config.toml — ai-wrapper auto-discovers on next request"
echo ""

if [[ "$_KEY_GENERATED" == "1" ]]; then
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  WRAPPER_API_KEY (auto-generated — save this now):${NC}"
    echo ""
    echo "    ${WRAPPER_API_KEY}"
    echo ""
    echo -e "${YELLOW}  Use it as:  Authorization: Bearer ${WRAPPER_API_KEY}${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
fi
