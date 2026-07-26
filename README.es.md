# llama-cpp — Electrodoméstico de IA para NUC

Un electrodoméstico de IA auto-alojado con aceleración GPU para un host
uCore headless (Fedora CoreOS + Podman CDI) con una eGPU NVIDIA RTX 3060
12 GB. Expone un único endpoint compatible con OpenAI con builds
deterministas, gestión de engines por hot-swap y auto-inicio con systemd.

[Readme in English](README.md)

---

## Arquitectura

```
Cliente (Odysseus / cualquier cliente compatible con OpenAI)
  │
  ▼  puerto 5128 (Tailscale TCP serve)
ai-wrapper                        ← proxy OpenAI + Director de VRAM
  │  solo UN engine activo a la vez (guard asyncio)
  │  detiene el engine activo → 1.5 s liberación VRAM → arranca el nuevo
  │
  ├─▶ llama-cuda    :5121   ← ggerganov/llama.cpp upstream
  ├─▶ llama-atomic  :5122   ← fork con speculative decoding
  └─▶ llama-tom     :5123   ← fork con TurboQuant KV-cache 3-bit
        │
        └── modelos desde NVMe del host (data/models/<nombre>/)
```

**Esquema de puertos:**

| Servicio    | Puerto | Rol |
|-------------|--------|------|
| odysseus    | 5120   | Web UI (externo, opcional) |
| llama-cuda  | 5121   | Engine (upstream, inactivo hasta que se pida) |
| llama-atomic| 5122   | Engine (speculative, inactivo hasta que se pida) |
| llama-tom   | 5123   | Engine (TurboQuant, inactivo hasta que se pida) |
| ai-wrapper  | 5128   | Proxy compatible con API de OpenAI |

Los engines son **efímeros** — `ai-wrapper` lanza uno a la vez vía el
socket Podman del host con `--device nvidia.com/gpu=all` (CDI). Solo un
engine tiene la GPU en cada momento.

---

## Inicio rápido

### Requisitos

- uCore (Fedora CoreOS) con Podman
- Driver NVIDIA + spec CDI en `/etc/cdi/nvidia.yaml`
- RTX 3060 12 GB (Compute Capability 8.6) vía Thunderbolt 3
- Tailscale (opcional, para exposición en la tailnet)

### Instalación

```bash
git clone https://github.com/hbuddenberg/llama-cpp.git
cd llama-cpp
./nuc-infra/scripts/release.sh
```

El instalador (`release.sh`) **es el artefacto de release**. Este:
1. Verifica requisitos (CDI hard-gate, podman.socket, GPU)
2. Descarga las 4 imágenes pinneadas a `IMAGE_TAG` (default `v1.0.0`)
3. Genera `.env` con una API key aleatoria (`sk-llama-<hex>`)
4. Crea un Quadlet de Podman para `ai-wrapper` (auto-inicio con systemd)
5. Habilita `loginctl enable-linger` (arranca sin login)
6. Verifica el endpoint y lo expone en Tailscale

### Verificar

```bash
# Verificar el endpoint
curl http://localhost:5128/v1/models \
  -H "Authorization: Bearer $(grep WRAPPER_API_KEY nuc-infra/.env | cut -d= -f2)"

# Probar un completion
curl http://localhost:5128/v1/chat/completions \
  -H "Authorization: Bearer sk-llama-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-14b","messages":[{"role":"user","content":"Hola"}]}'
```

---

## Cómo usarlo

### Gestionar el servicio

```bash
systemctl --user status ai-wrapper       # ver estado
systemctl --user restart ai-wrapper      # reiniciar
systemctl --user stop ai-wrapper         # detener
journalctl --user -u ai-wrapper -f       # logs en vivo
```

### Actualizar a un nuevo release

Editar `IMAGE_TAG` al principio de `nuc-infra/scripts/release.sh` y volver a ejecutar:

```bash
./nuc-infra/scripts/release.sh
```

### Volver al release anterior

```bash
./nuc-infra/scripts/release.sh --rollback
```

El estado se registra en `~/.local/state/llama-cpp/releases.log` (JSONL con
digests de imágenes para rollback byte-idéntico).

### Agregar un modelo

Crear una carpeta bajo `nuc-infra/data/models/`:

```
data/models/mi-modelo/
  ├── model.gguf       ← los pesos
  └── config.toml      ← engine + argumentos
```

Ejemplo de `config.toml`:

```toml
[model]
alias  = "mi-modelo"
engine = "llama-cuda"     # o llama-atomic / llama-tom
file   = "model.gguf"

[args]
ctx_size     = 4096
n_gpu_layers = 99
flash_attn   = true
```

No requiere cambiar código ni compose. `ai-wrapper` descubre los modelos
por mtime.

### Generar API keys adicionales

```bash
curl -X POST http://localhost:5128/admin/keys \
  -H "Authorization: Bearer <bootstrap-key>"
```

Devuelve `{"key":"sk-llama-<random>"}`. Revocar con `DELETE /admin/keys/<key>`.

### Descargar modelos

```bash
./nuc-infra/scripts/fetch-models.sh
```

Requiere `huggingface-cli` (`pip install -U 'huggingface_hub[cli]'`).

---

## Componentes

| Directorio | Descripción | Imagen |
|------------|-------------|--------|
| [`nuc-infra/`](nuc-infra/) | Orquestación, configs, scripts (release.sh) | — |
| [`ai-wrapper/`](ai-wrapper/) | Director de VRAM FastAPI + proxy OpenAI | `ghcr.io/hbuddenberg/ai-wrapper` |
| [`llama-cuda/`](llama-cuda/) | ggerganov/llama.cpp upstream (CUDA, solo API) | `ghcr.io/hbuddenberg/llama-cuda` |
| [`llama-atomic/`](llama-atomic/) | Fork con speculative decoding (CUDA, solo API) | `ghcr.io/hbuddenberg/llama-atomic` |
| [`llama-tom/`](llama-tom/) | Fork con TurboQuant KV-cache 3-bit (CUDA, solo API) | `ghcr.io/hbuddenberg/llama-tom` |

Todas las imágenes de engines son **solo API** (sin web UI): construidas
con un stub `ui.h`, `-DLLAMA_BUILD_UI=OFF`, `-DGGML_NATIVE=OFF` (portable,
sin AVX512), `SOURCE_REF` pinneado a SHA para builds deterministas.

---

## CI/CD

Cada componente tiene un workflow de GitHub Actions
(`.github/workflows/deploy.yml`) que construye y sube a GHCR en push a `main`:

| Componente | Imagen |
|------------|--------|
| ai-wrapper | `ghcr.io/hbuddenberg/ai-wrapper:latest` |
| llama-cuda | `ghcr.io/hbuddenberg/llama-cuda:latest` |
| llama-atomic | `ghcr.io/hbuddenberg/llama-atomic:latest` |
| llama-tom | `ghcr.io/hbuddenberg/llama-tom:latest` |

Los builds son deterministas — `SOURCE_REF` está pinneado a un commit SHA
en cada Dockerfile (nunca `main`). Para actualizar un engine, editar el SHA
y hacer push.

---

## Seguridad

- Todas las peticiones a la API requieren un Bearer token (`sk-llama-<hex>`)
- Multi-key store: la key bootstrap genera keys admin vía `POST /admin/keys`
- Fail-closed: si no hay key y `ALLOW_ANONYMOUS` no está seteado, el wrapper se niega a arrancar
- El Quadlet usa `EnvironmentFile=` (secretos no visibles vía `systemctl show`)
- SELinux: los containers usan `security_opt: label=disable` para el socket podman + GPU
- Todos los mounts de Podman usan `:z` o `:ro,z`

---

## Licencia

MIT
