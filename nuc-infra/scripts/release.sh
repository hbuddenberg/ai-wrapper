#!/usr/bin/env bash
# release.sh — Self-contained installer for the AI appliance.
#
# This script IS the release artifact. IMAGE_TAG is pinned below. Running it
# on a uCore host with an NVIDIA GPU produces a working OpenAI-compatible
# endpoint with auto-start, deterministically.
#
# Architecture:
#   - ai-wrapper (always-on, Quadlet-managed) on port 5128
#   - 3 engine images (idle in local registry, launched on-demand by ai-wrapper)
#   - VRAM isolation: only one engine runs at a time
#
# Usage:
#   ./release.sh                 Install or update
#   ./release.sh --rollback      Revert to previous release
#   ./release.sh --skip-models   Don't warn about missing models
#   ./release.sh --help          Show this help

set -euo pipefail

# =====================================================================
#  RELEASE METADATA — single source of truth.
#  Bump IMAGE_TAG to cut a new release. That's it.
#  NOTE: CI currently publishes :latest. To use version tags, update the
#  workflows to also tag images on git tag pushes. Until then, rollback
#  is digest-based (state file records digests for byte-identical recall).
# =====================================================================
IMAGE_TAG="latest"
GH_USER="hbuddenberg"
REGISTRY="ghcr.io"

IMAGES=(
  "${REGISTRY}/${GH_USER}/ai-wrapper:${IMAGE_TAG}"
  "${REGISTRY}/${GH_USER}/llama-cuda:${IMAGE_TAG}"
  "${REGISTRY}/${GH_USER}/llama-atomic:${IMAGE_TAG}"
  "${REGISTRY}/${GH_USER}/llama-tom:${IMAGE_TAG}"
)

NETWORK_NAME="ai-isolated-net"

# =====================================================================
#  PATHS
# =====================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NUC_INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${NUC_INFRA_DIR}/data"
MODELS_DIR="${DATA_DIR}/models"
KEYS_FILE="${DATA_DIR}/api_keys.txt"
ENV_FILE="${NUC_INFRA_DIR}/.env"

STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/llama-cpp"
STATE_FILE="${STATE_DIR}/releases.log"

QUADLET_DIR="${HOME}/.config/containers/systemd"
QUADLET_FILE="${QUADLET_DIR}/ai-wrapper.container"

PODMAN_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"

# =====================================================================
#  HELPERS
# =====================================================================
log()   { printf '\033[32m[OK]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[33m[WARN]\033[0m  %s\n' "$*"; }
die()   { printf '\033[31m[ERR]\033[0m   %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[36m━━━ %s ━━━\033[0m\n' "$*"; }

gen_key() { printf 'sk-llama-%s' "$(openssl rand -hex 24)"; }

# =====================================================================
#  PHASE 1 — PRE-FLIGHT (hard-fail on missing requirements)
# =====================================================================
preflight() {
  step "Phase 1 / Pre-flight checks"

  # --- OS check ---
  if [[ -f /usr/lib/os-release ]]; then
    # shellcheck disable=SC1091
    source /usr/lib/os-release
  fi
  if [[ "${ID:-}" != "fedora" ]]; then
    die "Requires Fedora/uCore. Detected: ${ID:-unknown}"
  fi
  log "OS: ${PRETTY_NAME:-${ID}}"

  # --- Podman ---
  command -v podman >/dev/null 2>&1 || die "podman not found"
  log "podman $(podman --version 2>&1 | awk '{print $3}')"

  # --- NVIDIA CDI spec (HARD GATE) ---
  # Without this, --device nvidia.com/gpu=all fails with a cryptic OCI
  # runtime error. Better to die here with a clear message.
  if [[ ! -f /etc/cdi/nvidia.yaml ]]; then
    cat >&2 <<'MSG'
CDI spec missing: /etc/cdi/nvidia.yaml

Engines are launched with --device nvidia.com/gpu=all (CDI). Without the CDI
spec, the container runtime cannot discover the GPU.

Fix:
  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

Then re-run this script.
MSG
    die "CDI spec not found — cannot proceed."
  fi
  log "CDI spec: /etc/cdi/nvidia.yaml"

  # --- GPU visible ---
  if ! nvidia-smi --query-gpu=name,memory.total --format=csv,noheader >/dev/null 2>&1; then
    die "nvidia-smi failed — driver not loaded or GPU not connected"
  fi
  log "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"

  # --- Podman socket (rootless, user-scoped) ---
  if ! systemctl --user is-active podman.socket >/dev/null 2>&1; then
    systemctl --user enable --now podman.socket
  fi
  systemctl --user is-active podman.socket >/dev/null 2>&1 \
    || die "podman.socket would not start"
  log "podman.socket: active ($PODMAN_SOCK)"

  # --- Linger (boot without login) ---
  if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q yes; then
    loginctl enable-linger "$USER"
  fi
  log "linger: enabled for $USER"

  # --- Tailscale (optional but recommended) ---
  if command -v tailscale >/dev/null 2>&1; then
    log "tailscale: $(tailscale status --json 2>/dev/null | grep -o '"Online":true' >/dev/null && echo online || echo offline)"
  else
    warn "tailscale CLI not found — expose step will be skipped"
  fi
}

# =====================================================================
#  PHASE 2 — PULL IMAGES + STATE LOG
# =====================================================================
pull_images() {
  step "Phase 2 / Pull images ($IMAGE_TAG)"

  mkdir -p "$STATE_DIR"

  local digests=""
  for image in "${IMAGES[@]}"; do
    local short_name
    short_name="$(basename "${image%:*}")"
    printf '  pulling %-45s ' "$short_name"
    if ! podman pull "$image" >/dev/null 2>&1; then
      printf 'FAIL\n'
      die "Failed to pull $image"
    fi
    local digest
    digest="$(podman inspect --format '{{.Digest}}' "$image" 2>/dev/null || echo unknown)"
    printf '%s\n' "$digest"
    digests="${digests},\"${short_name}\":\"${digest}\""
  done

  # Append-only JSONL state log for rollback
  local release_id ts
  release_id="$(date +%Y%m%d-%H%M%S)-${IMAGE_TAG}"
  ts="$(date -Iseconds)"
  printf '{"release_id":"%s","ts":"%s","tag":"%s","digests":{%s}}\n' \
    "$release_id" "$ts" "$IMAGE_TAG" "${digests:2}" \
    >> "$STATE_FILE"

  log "State logged: $STATE_FILE ($release_id)"
}

# =====================================================================
#  PHASE 3 — CONFIGURE (idempotent: keep existing .env/keys)
# =====================================================================
configure() {
  step "Phase 3 / Configure environment"

  mkdir -p "$DATA_DIR"

  # --- .env ---
  local api_key
  if [[ -f "$ENV_FILE" ]] && grep -q '^WRAPPER_API_KEY=' "$ENV_FILE"; then
    api_key="$(grep '^WRAPPER_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
    log ".env exists — keeping current API key"
  else
    api_key="$(gen_key)"
    # Append missing keys — never truncate an existing .env.
    touch "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    grep -q '^GH_USER='         "$ENV_FILE" || printf 'GH_USER=%s\n'        "${GH_USER}"     >> "$ENV_FILE"
    grep -q '^WRAPPER_API_KEY=' "$ENV_FILE" || printf 'WRAPPER_API_KEY=%s\n' "${api_key}"    >> "$ENV_FILE"
    grep -q '^MODELS_HOST_DIR=' "$ENV_FILE" || printf 'MODELS_HOST_DIR=%s\n' "${MODELS_DIR}" >> "$ENV_FILE"
    log ".env configured (existing content preserved)"
  fi

  # --- api_keys.txt (bootstrap key store) ---
  if [[ ! -f "$KEYS_FILE" ]]; then
    printf '%s\n' "$api_key" > "$KEYS_FILE"
    chmod 600 "$KEYS_FILE"
    log "Created api_keys.txt with bootstrap key"
  else
    log "api_keys.txt exists — keeping current keys"
  fi

  # Export for quadlet generation
  API_KEY="$api_key"

  # --- Models ---
  if [[ ! -d "$MODELS_DIR" ]] || [[ -z "$(ls -A "$MODELS_DIR" 2>/dev/null)" ]]; then
    if [[ "$SKIP_MODELS" == true ]]; then
      warn "Models empty (--skip-models) — engines will fail until you fetch them"
    else
      warn "Models directory empty — run: $SCRIPT_DIR/fetch-models.sh"
    fi
  else
    local count
    count="$(find "$MODELS_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)"
    log "Models: $count entries in $MODELS_DIR"
  fi

  # --- Podman network ---
  if ! podman network exists "$NETWORK_NAME" 2>/dev/null; then
    podman network create "$NETWORK_NAME" >/dev/null
    log "Created network: $NETWORK_NAME"
  else
    log "Network exists: $NETWORK_NAME"
  fi
}

# =====================================================================
#  PHASE 4 — GENERATE QUADLET
# =====================================================================
generate_quadlet() {
  step "Phase 4 / Generate Quadlet (ai-wrapper)"

  mkdir -p "$QUADLET_DIR"

  cat > "$QUADLET_FILE" <<EOF
# Generated by release.sh — do not edit manually.
# Re-run release.sh to regenerate after updates.
# Image tag: ${IMAGE_TAG}
# Generated: $(date -Iseconds)

[Container]
Image=${REGISTRY}/${GH_USER}/ai-wrapper:${IMAGE_TAG}
ContainerName=ai-wrapper
PublishPort=5128:8000
SecurityLabelDisable=true

# Rootless podman socket — ai-wrapper uses it to launch engines.
Volume=${PODMAN_SOCK}:/run/podman/podman.sock

# Models (read-only) and multi-key store.
Volume=${MODELS_DIR}:/models:ro,z
Volume=${KEYS_FILE}:/app/api_keys.txt:z

# Secrets from .env (EnvironmentFile — not visible via systemctl show).
EnvironmentFile=${ENV_FILE}

# Internal container paths only (no secrets here).
Environment=PODMAN_URL=unix:///run/podman/podman.sock
Environment=MODELS_DIR=/models
Environment=ENGINE_NETWORK=${NETWORK_NAME}

# Network: engines join this network when launched.
Network=${NETWORK_NAME}

[Service]
Restart=always
RestartSec=5
TimeoutStartSec=120

[Install]
WantedBy=default.target
EOF

  chmod 644 "$QUADLET_FILE"
  systemctl --user daemon-reload
  log "Quadlet: $QUADLET_FILE"
}

# =====================================================================
#  PHASE 5 — ENABLE + START
# =====================================================================
enable_services() {
  step "Phase 5 / Enable auto-start"

  systemctl --user enable --now ai-wrapper.service
  log "ai-wrapper.service: enabled + started"
}

# =====================================================================
#  PHASE 6 — HEALTH CHECK
# =====================================================================
health_check() {
  step "Phase 6 / Health check"

  printf '  waiting for ai-wrapper'
  local i
  for ((i = 0; i < 30; i++)); do
    if curl -sf "http://localhost:5128/v1/models" \
         -H "Authorization: Bearer ${API_KEY}" >/dev/null 2>&1; then
      printf ' ready!\n'
      log "GET /v1/models → 200"
      return 0
    fi
    printf '.'
    sleep 2
  done
  printf ' TIMEOUT\n'
  die "ai-wrapper not responding after 60s

Check logs:  journalctl --user -u ai-wrapper -n 50 --no-pager
Check status: systemctl --user status ai-wrapper"
}

# =====================================================================
#  PHASE 7 — TAILSCALE EXPOSE + SUMMARY
# =====================================================================
expose_and_summary() {
  step "Phase 7 / Expose + Summary"

  local ts_hostname=""
  if command -v tailscale >/dev/null 2>&1; then
    # Expose port 5128 on the tailnet
    if tailscale serve status 2>/dev/null | grep -q 5128; then
      log "tailscale serve already exposing :5128"
    else
      tailscale serve --bg --tcp 5128 "tcp://127.0.0.1:5128" 2>/dev/null \
        && log "tailscale serve: :5128 exposed" \
        || warn "tailscale serve failed (non-fatal)"
    fi
    ts_hostname="$(tailscale status --json 2>/dev/null \
      | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  fi

  local engine_imgs=""
  for img in llama-cuda llama-atomic llama-tom; do
    local d
    d="$(podman inspect --format '{{.Digest}}' "${REGISTRY}/${GH_USER}/${img}:${IMAGE_TAG}" 2>/dev/null || echo "?")"
    engine_imgs="${engine_imgs}    ${img}:  ${d:0:19}\n"
  done

  # Write full API key to a protected file (print only a truncated hint).
  local key_file="${DATA_DIR}/.initial_key"
  printf '%s\n' "$API_KEY" > "$key_file"
  chmod 600 "$key_file"
  local key_hint="sk-llama-…${API_KEY: -4}"

  printf '\n'
  printf '  ┌──────────────────────────────────────────────────┐\n'
  printf '  │           AI APPLIANCE — DEPLOYED                │\n'
  printf '  ├──────────────────────────────────────────────────┤\n'
  printf '  │ Tag:        %-37s│\n' "$IMAGE_TAG"
  printf '  │ API Key:    %-37s│\n' "$key_hint (full: data/.initial_key)"
  printf '  │                                                  │\n'
  printf '  │ Local:      http://localhost:5128/v1/models       │\n'
  if [[ -n "$ts_hostname" ]]; then
    printf '  │ Tailscale:  http://%s:5128/v1/models\n' "$ts_hostname"
  fi
  printf '  │                                                  │\n'
  printf '  │ Engines (idle in registry, tag %s):          │\n' "$IMAGE_TAG"
  printf "${engine_imgs}"
  printf '  │                                                  │\n'
  printf '  │ Manage:     systemctl --user {status|restart|stop} ai-wrapper\n'
  printf '  │ Logs:       journalctl --user -u ai-wrapper -f    │\n'
  printf '  │ Rollback:   %s --rollback\n' "$(basename "$0")"
  printf '  └──────────────────────────────────────────────────┘\n'
  printf '\n'
}

# =====================================================================
#  ROLLBACK
# =====================================================================
rollback() {
  step "Rollback to previous release"

  [[ -f "$STATE_FILE" ]] || die "No release history at $STATE_FILE"

  local line_count
  line_count="$(wc -l < "$STATE_FILE")"
  if [[ "$line_count" -lt 2 ]]; then
    die "Only one release in history — nothing to roll back to"
  fi

  # Read penultimate line (the release before current)
  local prev_line
  prev_line="$(tail -2 "$STATE_FILE" | head -1)"

  local prev_tag
  prev_tag="$(printf '%s' "$prev_line" | grep -o '"tag":"[^"]*"' | cut -d'"' -f4)"

  [[ -n "$prev_tag" ]] || die "Could not parse previous tag from state file"

  warn "Rolling back: $IMAGE_TAG → $prev_tag"

  local IMAGE_TAG_ORIG="$IMAGE_TAG"
  IMAGE_TAG="$prev_tag"

  # Re-pull previous image versions
  local image short_name
  for image in "${IMAGES[@]}"; do
    short_name="$(basename "${image%:*}")"
    printf '  pulling %-45s ' "$short_name"
    if podman pull "${image%:*}:${prev_tag}" >/dev/null 2>&1; then
      printf 'OK\n'
    else
      printf 'FAIL\n'
      die "Cannot pull ${image%:*}:${prev_tag} — image may be pruned from registry"
    fi
  done

  # Regenerate quadlet + restart
  generate_quadlet
  systemctl --user restart ai-wrapper.service
  health_check

  # Log the rollback so the audit trail stays intact and subsequent
  # --rollback calls target the correct penultimate entry.
  local rb_id rb_ts
  rb_id="$(date +%Y%m%d-%H%M%S)-${prev_tag}-rollback"
  rb_ts="$(date -Iseconds)"
  printf '{"release_id":"%s","ts":"%s","tag":"%s","rollback_from":"%s"}\n' \
    "$rb_id" "$rb_ts" "$prev_tag" "$IMAGE_TAG_ORIG" >> "$STATE_FILE"

  log "Rollback complete: now on $prev_tag"
}

# =====================================================================
#  MAIN
# =====================================================================
SKIP_MODELS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rollback)     ROLLBACK=true;     shift ;;
    --skip-models)  SKIP_MODELS=true;  shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    --help|-h)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) die "Unknown flag: $1" ;;
  esac
done

# --- Pre-flight always runs (even for rollback, to catch env issues) ---
preflight

if [[ "${ROLLBACK:-false}" == true ]]; then
  rollback
  exit 0
fi

pull_images
configure
generate_quadlet
enable_services
health_check
expose_and_summary
