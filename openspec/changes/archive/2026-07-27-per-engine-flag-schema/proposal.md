# Proposal: Per-Engine Flag Schema

## Intent
Extend `ai-wrapper` and engine Dockerfiles to discover and validate `llama-server` CLI arguments against baked `/etc/llama-engine/flags.toml` schemas per engine image (`llama-atomic`, `llama-cuda`). This prevents invalid CLI flags in `config.toml` from triggering opaque 503 timeout errors during VRAM Director hotswaps.

## Scope

### In Scope
- `ai-wrapper/main.py`: add `get_engine_schema` (lazy, digest-cached, CPU-only container inspection), extend `_validate_args` with schema fallback, and update `build_engine_command`.
- `llama-atomic/Dockerfile` & `llama-cuda/Dockerfile`: build-time `llama-server --help | extract_flags.py` to produce `/etc/llama-engine/flags.toml`.
- New `extract_flags.py` parser script.
- First pytest suite and `conftest.py` for `ai-wrapper`.

### Out of Scope
- `llama-tom/` image schema discovery (deferred due to prebuilt binary distribution).
- Moving base CLI flags into schema validation (wrapper-specific flags remain in code).
- `SOURCE_REF`/CUDA version bumps or host infra configuration updates.

## Capabilities

### New Capabilities
- `engine-flag-schema`: `ai-wrapper` discovers, digest-caches, and validates `[args]` against per-engine `/etc/llama-engine/flags.toml` schemas.
- `engine-flag-discovery`: Engine containers build `/etc/llama-engine/flags.toml` during image construction from `llama-server --help`.

### Modified Capabilities
None.

## Approach
Three-layer validation strategy:
1. Base configuration keys remain hard-coded with existing key mappings.
2. Per-engine schema lazily retrieved from container `/etc/llama-engine/flags.toml` on first swap and cached by container image digest.
3. Passthrough `extra` flags validated against the active engine's supported schema.

Images without `flags.toml` fall back to legacy allowlist validation with a warning log. Container extraction uses CPU-only invocation to adhere to VRAM isolation policies.

## Affected Areas
| Area | Impact | Change |
|------|--------|--------|
| `ai-wrapper/main.py` | Modified | Add `get_engine_schema`, extend `_validate_args` and `build_engine_command` |
| `ai-wrapper/tests/` | New | Initial pytest suite (`test_main.py`, `conftest.py`) |
| `llama-atomic/Dockerfile` | Modified | Add build-time `--help` flag extraction to `/etc/llama-engine/flags.toml` |
| `llama-cuda/Dockerfile` | Modified | Add build-time `--help` flag extraction to `/etc/llama-engine/flags.toml` |
| `extract_flags.py` | New | CLI `--help` output parser script |

## Risks & Mitigation
| Risk | Severity | Mitigation |
|------|----------|------------|
| `llama-server --help` fails in container build without GPU | High | Verify `--help` runs in CPU mode during build; fallback to host pre-generation if needed. |
| `--help` output format changes across upstream releases | Medium | Make `extract_flags.py` fail build explicitly on unexpected formatting. |
| Tag mutability (`:latest`) leads to stale schema cache | Low | Cache schema by container image digest rather than image tag. |

## Rollback Plan
Revert `ai-wrapper/main.py` modifications. Missing `/etc/llama-engine/flags.toml` automatically falls back to legacy `_ARGS_ALLOWLIST` behavior without breaking engine hotswaps.

## Success Criteria
- Invalid CLI flags in model `[args]` return immediate 503 validation errors rather than timeout errors.
- Engines without `flags.toml` (e.g. `llama-tom`) continue operating via legacy allowlist.
- Pytest suite passes for schema parsing, validation, and command construction.
- Container GPU and compilation flags remain unchanged.
