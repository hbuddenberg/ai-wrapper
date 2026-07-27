# Technical Design: Per-Engine Flag Schema

## Architecture Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Schema Generation | Build-time `llama-server --help \| extract_flags.py` | Bakes schema into image; handles fork differences without host runtime tools. |
| Stub Execution | `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs` in builder | Allows `llama-server --help` to run during build without physical GPU access. |
| Schema Location | `/etc/llama-engine/flags.toml` inside engine image | Standardized location across engine images (`llama-atomic`, `llama-cuda`). |
| Cache Key | Image Digest (`sha256:...`) | Prevents stale schema bugs from tag mutability (`:latest`). |
| Inspection Execution | `podman run --rm --entrypoint /bin/cat <image> /etc/...` | CPU-only execution; strictly preserves VRAM isolation hard rule. |
| Fallback Strategy | Base allowlist + warning log | Ensures prebuilt images (e.g. `llama-tom`) operate without breaking. |
| Rejection Timing | Fast HTTP 503 before engine swap | Prevents 30s health-check timeouts and unnecessary VRAM cooldowns. |

## Data Flow

```
+-----------------------------------------------------------------------------------+
| BUILD TIME (llama-atomic / llama-cuda Dockerfiles)                                |
|                                                                                   |
|  builder stage:                                                                   |
|  llama-server --help  --->  extract_flags.py  ---> /etc/llama-engine/flags.toml  |
|                                                            |                      |
|  runtime image stage:                                      v                      |
|  COPY --from=builder /etc/llama-engine/flags.toml /etc/llama-engine/flags.toml   |
+-----------------------------------------------------------------------------------+
                                                             |
+------------------------------------------------------------v----------------------+
| RUNTIME (ai-wrapper / main.py)                                                    |
|                                                                                   |
| 1. Scan config.toml   ---> Layer 1: Base Allowlist Check (ctx_size, n_gpu_layers...) |
| 2. On Swap Request    ---> Get Image Digest via podman inspect                     |
|                       ---> Schema Cache Hit?                                      |
|                             YES -> Use cached flag set                            |
|                             NO  -> podman run --entrypoint /bin/cat (CPU-only)    |
|                                    Parse flags.toml -> Cache by digest            |
| 3. Validate Extra     ---> Layer 2/3: Verify extra flags against schema           |
|                       ---> Invalid? HTTP 503 Fast Fail                             |
|                       ---> Valid?   Launch GPU engine container                       |
+-----------------------------------------------------------------------------------+
```

## File Changes & Implementation Details

| Component | Target File | Description of Changes |
|---|---|---|
| Flag Extractor | `extract_flags.py` | New CLI `--help` parser script. Extracts long (`--flag`) and short (`-f`) flags into TOML array. Exits non-zero if input is invalid or yields < 10 flags. |
| Atomic Engine | `llama-atomic/Dockerfile` | Installs `python3` in builder. Executes `llama-server --help \| extract_flags.py /etc/llama-engine/flags.toml` with `LD_LIBRARY_PATH` stubs. Copies TOML to runtime image. |
| CUDA Engine | `llama-cuda/Dockerfile` | Same builder extraction & runtime `COPY` as `llama-atomic`. Preserves all CUDA architectures and pinned `SOURCE_REF` values. |
| Wrapper Core | `ai-wrapper/main.py` | Implements `get_engine_schema` (lazy digest-cached CPU container inspection), extends `_validate_args` & `acquire_engine` with 3-layer validation, returns 503 on schema mismatch. |
| Test Suite | `ai-wrapper/tests/` | New `test_main.py` and `conftest.py` testing parser script, 3-layer validation, digest caching, fast 503 rejection, and legacy fallbacks. |

### Component Specifications

1. **`extract_flags.py`**:
   - Parses flags using regex matching `-s, --long` or `--long` patterns from stdin/file.
   - Generates TOML format: `[flags]\nflags = ["--ctx-size", "-c", ...]`.
   - Exits status `1` if input format is unrecognized or flag count is unexpectedly low.

2. **Dockerfile Builder Stages**:
   - Executes: `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs /src/build/bin/llama-server --help 2>/dev/null | python3 extract_flags.py /etc/llama-engine/flags.toml`
   - Copies `/etc/llama-engine/flags.toml` to Stage 2 runtime image.

3. **`ai-wrapper/main.py` Logic**:
   - `_schema_cache: dict[str, set[str]] = {}`
   - `get_engine_schema(engine: str)`: Inspects image digest using `podman image inspect`. If cached, returns flag set. Else runs `podman run --rm --entrypoint /bin/cat <image> /etc/llama-engine/flags.toml` without `--device nvidia.com/gpu=all`.
   - Fast 503 Rejection: In `acquire_engine`, before stopping active engine, validates `[args].extra` against schema + base allowlist. Raises HTTP 503 if an unknown flag is present.

## Threat Matrix

| Threat | Impact | Risk Level | Mitigation Strategy |
|---|---|---|---|
| Command Injection | Container command execution | High | Strict regex `_EXTRA_FLAG_RE`/`_EXTRA_BARE_RE` with schema whitelist matching. List-based subprocess execution (no `shell=True`). |
| VRAM Isolation Leak | GPU contention during schema read | High | Container run for schema inspection strictly omits `--device nvidia.com/gpu=all` (CPU-only `cat`). |
| Path Traversal | Arbitrary container path access | Medium | Strict check on `file` and `draft_model` prohibiting `/`, `\\`, and `..`. Path rooted under `/models/<folder>/`. |
| Tag Poisoning / Stale Cache | Bypass validation on updated image | Medium | Cache schemas keyed by container image **digest** (`sha256:...`) instead of mutable image tags (`:latest`). |
| Malformed Help Extraction | Silently under-validated CLI flags | Medium | `extract_flags.py` enforces loud failure (exit code 1) on unexpected `--help` output, breaking Docker build. |

## Testing Strategy

| Test Category | Target Scope | Verification Method |
|---|---|---|
| Extraction Parser | `extract_flags.py` | Unit test with valid `--help` samples, empty text, and invalid output. Verify non-zero exit on failure. |
| Digest Schema Cache | `ai-wrapper/main.py` | Unit test `get_engine_schema`: verify CPU-only Podman call, digest key assignment, and hit on subsequent calls. |
| 3-Layer Validation | `ai-wrapper/main.py` | Unit test `_validate_args` with base allowlist, schema flags, valid `extra` values, and invalid flags. |
| Fast Rejection | Fast API Endpoint | Integration test swap with invalid `extra` flag (`--invalid-flag`). Assert fast HTTP 503 before engine stop. |
| Legacy Fallback | Engine without schema | Test swap for image without `flags.toml` (e.g. `llama-tom`). Assert warning logged and base validation applied. |
