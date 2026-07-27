# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Workspace for a distributed AI appliance targeting a headless uCore host
(Fedora CoreOS + Podman CDI) with an i3 8th Gen, 16GB RAM and an RTX 3060
 12GB eGPU (Compute Capability 8.6, Thunderbolt 3). It is **one git
repository** (`github.com/hbuddenberg/llama-cpp`) with five top-level
components, each built and published by its own path-filtered GitHub
Action (no nested `.git`, no per-subdir remote):

- `nuc-infra/` — orchestration only (podman-compose, configs, scripts). Cloned onto the host.
- `ai-wrapper/` — FastAPI "VRAM Director": the single OpenAI-compatible endpoint (port 5120 on the host, published on the tailnet via `tailscale serve`).
- `llama-atomic/` — CUDA build pipeline for the speculative-decoding llama.cpp fork.
- `llama-tom/` — CUDA build pipeline for the TurboQuant 3-bit KV-cache llama.cpp fork.
- `llama-cuda/` — CUDA build pipeline for upstream `ggerganov/llama.cpp` (general-purpose engine).

Everything lives in this single repo — no nested `.git`, no per-subdir
remote. The `*/.github/workflows/deploy.yml` files inside each subdir are
vestigial; the active CI is the path-filtered set at `.github/workflows/`.

## Architecture

Request flow: client (LibreChat / Hermes on nuc-ai / any OpenAI client) →
`ai-wrapper:5120/v1` → active engine container (`llama-engine`, port 8080).

Hard rules:
- **VRAM isolation**: the engine containers must NEVER run concurrently. ai-wrapper
  stops the active engine, sleeps 1.5s (NVIDIA driver eGPU memory release),
  then `podman run`s the target via the host Podman socket with
  `--device nvidia.com/gpu=all` (CDI). Engines are NOT compose services.
- **Model autodiscovery**: each folder in `nuc-infra/data/models/` holds a
  GGUF + `config.toml` (alias, engine, llama-server args). ai-wrapper scans
  it (mtime-based) and builds its registry; adding a model requires no code
  or compose change.
- **RAM budget**: infra < 1.2GB at idle (MongoDB capped with
  `--wiredTigerCacheSizeGB 0.25`; engines don't run at idle).
- All Podman volume mounts use SELinux flags (`:z` or `:ro,z`).

## Commands

```bash
# validate compose
podman-compose -f nuc-infra/podman-compose.yml config

# syntax-check the wrapper
python -m py_compile ai-wrapper/main.py

# local build (engines are heavy CUDA builds — normally done in CI)
podman build -t ai-wrapper ./ai-wrapper

# deploy on host (from nuc-infra/)
cp env.example .env && podman-compose up -d
./scripts/fetch-models.sh && ./scripts/tailscale-expose.sh
```

CI: four path-filtered workflows in `.github/workflows/` (one per buildable
component — `deploy-ai-wrapper.yml`, `deploy-llama-atomic.yml`,
`deploy-llama-tom.yml`, `deploy-llama-cuda.yml`) each push
`ghcr.io/<owner>/<component>:latest` on push to main when the matching path
changes. (`nuc-infra/` is orchestration-only and has no image.) The
`*/.github/workflows/deploy.yml` files inside each subdir are vestigial.

Commits: conventional commits, no AI attribution.
