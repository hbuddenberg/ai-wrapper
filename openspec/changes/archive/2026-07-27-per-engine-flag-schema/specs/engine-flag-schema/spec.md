# engine-flag-schema Specification

## Purpose

Defines how `ai-wrapper/main.py` discovers each engine image's supported `llama-server` flags, validates a model's `[args]` table against three layers (base allowlist + per-engine schema + `extra` valve), caches schemas by image digest, and builds the engine command line. This capability EXTENDS — does not replace — the current fixed-allowlist behavior.

## Requirements

### Requirement: REQ-001 Three-Layer Argument Validation

The system MUST validate each model's `[args]` against three layers: (a) the hard-coded base allowlist (`ctx_size`, `n_gpu_layers`, `flash_attn`, `draft_model`, `draft_max`, `draft_min`, `load_timeout`, `extra`) with unchanged translations in `build_engine_command` (main.py:189-209); (b) optional per-engine flags declared in the image's `/etc/llama-engine/flags.toml`; (c) the `extra` regex valve (`_EXTRA_FLAG_RE`/`_EXTRA_BARE_RE`, main.py:72-73). The base allowlist and its translations MUST remain unchanged by this change.

#### Scenario: Happy path — schema present, args valid

- GIVEN a `llama-atomic` model whose image bakes `flags.toml` and whose `[args]` keys are all in the base allowlist or the per-engine schema
- WHEN ai-wrapper validates the model and builds the engine command
- THEN the model is admitted to the registry AND `build_engine_command` emits the base translations plus any schema-driven optional flags

#### Scenario: Structural rejection at scan time

- GIVEN a model whose `[args]` contains a key that is not in the base allowlist (e.g. `unknown_key = 1`)
- WHEN `scan_registry` runs `_validate_args` (main.py:91-115)
- THEN the model is excluded from the registry AND the error log names the offending key

#### Scenario: Cross-fork rejection at swap time

- GIVEN a `llama-cuda` model whose `[args].extra` references a flag declared ONLY in `llama-atomic`'s `flags.toml` (an atomic-fork-specific flag absent from upstream `llama-server`)
- WHEN ai-wrapper attempts the swap
- THEN the swap is rejected for that model with a clear error naming the flag — NOT a health-check timeout at `load_timeout`

### Requirement: REQ-002 Lazy Digest-Keyed Schema Cache

The system MUST fetch each engine's `flags.toml` lazily on first swap and cache it keyed by image **digest** (not the mutable `:latest` tag). The fetch MUST run a CPU-only container via `podman run --rm --entrypoint /bin/cat <image> /etc/llama-engine/flags.toml` WITHOUT `--device nvidia.com/gpu=all`, preserving the VRAM-isolation hard rule (CLAUDE.md).

#### Scenario: Cache hit — same digest

- GIVEN ai-wrapper has already cached the schema for image digest D
- WHEN a second model backed by the same digest is swapped in
- THEN the cached schema is reused AND no `podman run --entrypoint /bin/cat` is invoked

#### Scenario: Cache miss — different digest

- GIVEN the cached schema is for digest D1 and the target image's digest is D2 ≠ D1
- WHEN the swap proceeds
- THEN ai-wrapper re-reads `flags.toml` via the CPU-only `cat` container and updates the cache entry to D2

### Requirement: REQ-003 Legacy Fallback for Images Without `flags.toml`

If the target image has no `/etc/llama-engine/flags.toml`, the system MUST fall back to base + `extra` validation only, log a warning, and the model MUST remain eligible for the registry.

#### Scenario: Image without flags.toml falls back silently

- GIVEN an engine image (e.g. published `llama-tom`) with no `flags.toml`
- WHEN ai-wrapper resolves the schema for a model using that engine
- THEN a warning is logged, validation reduces to base + `extra`, AND the model is served exactly as today

### Requirement: REQ-004 Clear Rejection at Swap, Not Health-Check Timeout

Schema-layer rejection (a flag in `[args].extra` not present in the resolved schema) MUST surface as an HTTP 503 naming the offending flag — NOT as an opaque health-check timeout at `load_timeout` (main.py:266-281).

#### Scenario: Typo'd flag rejected fast

- GIVEN a model with `[args].extra = ["--cach-type-k", "q4_0"]` (typo, not in the resolved schema)
- WHEN the swap is attempted
- THEN ai-wrapper returns 503 naming `--cach-type-k` before any health-check polling begins

## Non-Goals

- `llama-tom/` engine schema discovery (deferred to a follow-up change; runtime-variant image has no builder stage for `--help`).
- Moving the 6 base CLI translations (`-c`, `-ngl`, `--flash-attn on`, `--model-draft`, `--draft-max`, `--draft-min`) into `flags.toml` — they stay hard-coded.
- `SOURCE_REF`/CUDA bumps, root workflow dedup, `nuc-infra/` config changes.
