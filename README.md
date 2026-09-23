# llama-cpp — NUC AI Appliance

A self-hosted, GPU-accelerated AI appliance for a headless uCore host
(Fedora CoreOS + Podman CDI) with an NVIDIA RTX 3060 12 GB eGPU.
Exposes a single OpenAI-compatible endpoint with deterministic builds,
hot-swap engine management, and systemd auto-start.

[README en español](README.es.md)

---

## Architecture

```
Client (Odysseus / any OpenAI-compatible client)
  │
  ▼  port 5128 (Tailscale TCP serve)
ai-wrapper                        ← OpenAI proxy + VRAM Director
  │  only ONE engine active at a time (asyncio guard)
  │  stops active engine → 1.5 s VRAM release → starts target
  │
  ├─▶ llama-cuda    :5121   ← upstream ggerganov/llama.cpp
  ├─▶ llama-atomic  :5122   ← speculative decoding fork
  └─▶ llama-tom     :5123   ← TurboQuant 3-bit KV-cache fork
        │
        └── models from host NVMe (data/models/<name>/)
```

**Port scheme:**

| Service  | Port | Role |
|----------|------|------|
| odysseus | 5120 | Web UI (external, optional) |
| llama-cuda | 5121 | Engine (upstream, idle until requested) |
| llama-atomic | 5122 | Engine (speculative, idle until requested) |
| llama-tom | 5123 | Engine (TurboQuant, idle until requested) |
| ai-wrapper | 5128 | OpenAI-compatible API proxy |

Engines are **ephemeral** — `ai-wrapper` launches one at a time via the
host Podman socket with `--device nvidia.com/gpu=all` (CDI). Only one
engine holds the GPU at any time.

---

## Quick Start

### Prerequisites

- uCore (Fedora CoreOS) with Podman
- NVIDIA driver + CDI spec at `/etc/cdi/nvidia.yaml`
- RTX 3060 12 GB (Compute Capability 8.6) via Thunderbolt 3
- Tailscale (optional, for tailnet exposure)

### Install

```bash
git clone https://github.com/hbuddenberg/ai-wrapper.git
cd llama-cpp
./nuc-infra/scripts/release.sh
```

The installer (`release.sh`) is the **release artifact itself**. It:
1. Verifies prerequisites (CDI hard-gate, podman.socket, GPU)
2. Pulls all 4 images pinned to `IMAGE_TAG` (default `v1.0.0`)
3. Generates `.env` with a random API key (`sk-llama-<hex>`)
4. Creates a Podman Quadlet for `ai-wrapper` (systemd auto-start)
5. Enables `loginctl enable-linger` (boots without login)
6. Health-checks the endpoint and exposes it on Tailscale

### Verify

```bash
# Check the endpoint
curl http://localhost:5128/v1/models \
  -H "Authorization: Bearer $(grep WRAPPER_API_KEY nuc-infra/.env | cut -d= -f2)"

# Test a completion
curl http://localhost:5128/v1/chat/completions \
  -H "Authorization: Bearer sk-llama-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-14b","messages":[{"role":"user","content":"Hello"}]}'
```

---

## How-to

### Manage the service

```bash
systemctl --user status ai-wrapper      # check status
systemctl --user restart ai-wrapper      # restart
systemctl --user stop ai-wrapper         # stop
journalctl --user -u ai-wrapper -f       # live logs
```

### Update to a new release

Edit `IMAGE_TAG` at the top of `nuc-infra/scripts/release.sh`, then re-run:

```bash
./nuc-infra/scripts/release.sh
```

### Rollback to previous release

```bash
./nuc-infra/scripts/release.sh --rollback
```

State is tracked in `~/.local/state/llama-cpp/releases.log` (JSONL with
image digests for byte-identical rollback).

### Add a model

Create a folder under `nuc-infra/data/models/`:

```
data/models/my-model/
  ├── model.gguf       ← the weights
  └── config.toml      ← engine + args
```

Example `config.toml`:

```toml
[model]
alias  = "my-model"
engine = "llama-cuda"     # or llama-atomic / llama-tom
file   = "model.gguf"

[args]
ctx_size     = 4096
n_gpu_layers = 99
flash_attn   = true
```

No code or compose change needed. `ai-wrapper` discovers models by mtime.

### Mint additional API keys

```bash
curl -X POST http://localhost:5128/admin/keys \
  -H "Authorization: Bearer <bootstrap-key>"
```

Returns `{"key":"sk-llama-<random>"}`. Revoke with `DELETE /admin/keys/<key>`.

### Fetch models

```bash
./nuc-infra/scripts/fetch-models.sh
```

Requires `huggingface-cli` (`pip install -U 'huggingface_hub[cli]'`).

---

## Components

| Directory | Description | Image |
|-----------|-------------|-------|
| [`nuc-infra/`](nuc-infra/) | Orchestration, configs, scripts (release.sh) | — |
| [`ai-wrapper/`](ai-wrapper/) | FastAPI VRAM Director + OpenAI proxy | `ghcr.io/hbuddenberg/ai-wrapper` |
| [`llama-cuda/`](llama-cuda/) | Upstream ggerganov/llama.cpp (CUDA, API-only) | `ghcr.io/hbuddenberg/llama-cuda` |
| [`llama-atomic/`](llama-atomic/) | Speculative-decoding fork (CUDA, API-only) | `ghcr.io/hbuddenberg/llama-atomic` |
| [`llama-tom/`](llama-tom/) | TurboQuant 3-bit KV-cache fork (CUDA, API-only) | `ghcr.io/hbuddenberg/llama-tom` |

All engine images are **API-only** (no web UI): built with a stub `ui.h`,
`-DLLAMA_BUILD_UI=OFF`, `-DGGML_NATIVE=OFF` (portable, no AVX512),
pinned `SOURCE_REF` SHA for deterministic builds.

---

## CI/CD

Each component has a GitHub Actions workflow (`.github/workflows/deploy.yml`)
that builds and pushes to GHCR on push to `main`:

| Component | Image |
|-----------|-------|
| ai-wrapper | `ghcr.io/hbuddenberg/ai-wrapper:latest` |
| llama-cuda | `ghcr.io/hbuddenberg/llama-cuda:latest` |
| llama-atomic | `ghcr.io/hbuddenberg/llama-atomic:latest` |
| llama-tom | `ghcr.io/hbuddenberg/llama-tom:latest` |

Builds are deterministic — `SOURCE_REF` is pinned to a commit SHA in each
Dockerfile (never `main`). To bump an engine, edit the SHA and push.

---

## Security

- All API requests require a Bearer token (`sk-llama-<hex>`)
- Multi-key store: bootstrap key mints admin keys via `POST /admin/keys`
- Fail-closed: if no key is set and `ALLOW_ANONYMOUS` is unset, the wrapper refuses to start
- Quadlet uses `EnvironmentFile=` (secrets not visible via `systemctl show`)
- SELinux: containers use `security_opt: label=disable` for podman socket + GPU access
- All Podman mounts use `:z` or `:ro,z`

---

## License

MIT
