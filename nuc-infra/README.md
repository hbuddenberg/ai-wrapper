# nuc-infra

Orchestration repository for the NUC AI appliance (uCore / Fedora CoreOS +
Podman CDI, RTX 3060 12GB eGPU). Contains no source code — only deployment
configuration. Clone it directly onto the host.

## Architecture

- `ai-wrapper` is the **single OpenAI-compatible endpoint** of the appliance
  (host port **5120**, also published on the tailnet). LibreChat, Hermes on
  nuc-ai and any other OpenAI client all talk to it.
- The inference engines (`llama-atomic`, `llama-tom`) are **not** compose
  services: ai-wrapper launches them on demand through the host Podman socket
  and guarantees only one ever holds the GPU (strict VRAM isolation with a
  1.5s cooldown between swaps).
- Models are auto-discovered: each folder under `data/models/` holds a GGUF
  plus a `config.toml` declaring its alias, engine and llama-server args.
  Adding a model = adding a folder. No code or compose changes.

## Setup

```bash
cp env.example .env            # then fill in the secrets
./scripts/fetch-models.sh      # download GGUF weights to data/models/
podman pull ghcr.io/$GH_USER/llama-atomic:latest
podman pull ghcr.io/$GH_USER/llama-tom:latest
podman-compose up -d
./scripts/tailscale-expose.sh  # publish port 5128 on the tailnet
tailscale funnel --bg 5128    # (optional) publish publicly at https://<hostname>.<tailnet>.ts.net
```

The rootless Podman socket must be enabled on the host:

```bash
systemctl --user enable --now podman.socket
```

To make the tailscale serve mapping persistent across reboots on uCore,
create a systemd unit that runs `scripts/tailscale-expose.sh` after
`tailscaled.service`.

## Endpoints

| Service    | URL                                     |
| ---------- | --------------------------------------- |
| LibreChat  | `http://<host>:3000`                    |
| AI API     | `http://<host>:5128/v1` (Bearer auth)   |
| AI API (tailnet) | `http://<tailnet-hostname>:5128/v1` |
| AI API (public, Funnel) | `https://<tailnet-hostname>.<tailnet>.ts.net/v1` (Bearer auth) |

## Memory budget (idle)

MongoDB is capped via `--wiredTigerCacheSizeGB 0.25`; engines do not run at
idle. Total infra target: < 1.2GB RAM.
