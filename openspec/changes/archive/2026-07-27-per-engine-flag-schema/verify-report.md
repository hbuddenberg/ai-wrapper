# Verification Report: per-engine-flag-schema

**Verdict**: PASS  
**Date**: 2026-07-27  
**Workspace**: `/var/home/hbuddenberg/developments/llama-cpp`  
**Persistence Mode**: `hybrid`  

---

## 1. Task Completeness Audit

All tasks specified in `openspec/changes/per-engine-flag-schema/tasks.md` are marked completed (`[x]`):

- [x] **Task 1.1: Build-Time Flag Extraction Script & RED Unit Test**
  - Created `nuc-infra/scripts/extract_flags.py` to parse `llama-server --help` output into TOML `[flags]` format.
  - Enforces minimum threshold of 10 flags; exits status code 1 on failure.
  - Unit tests created in `ai-wrapper/tests/test_extract_flags.py`.
- [x] **Task 1.2: Dockerfile Builder Capture Integration**
  - Updated `llama-atomic/Dockerfile` and `llama-cuda/Dockerfile` builder stages to execute `llama-server --help` with `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs` and parse via `extract_flags.py`.
  - Added Stage 2 `COPY --from=builder /etc/llama-engine/flags.toml /etc/llama-engine/flags.toml`.
- [x] **Task 2.1: RED Unit & Integration Test Suite (`ai-wrapper/tests/test_flags.py`)**
  - Added unit/integration tests covering CPU-only inspection, digest-keyed schema caching, 3-layer argument validation, command injection/path traversal security, fast HTTP 503 rejection, and legacy fallback.
- [x] **Task 2.2: Implement `get_engine_schema` and Lazy Digest Caching**
  - Added `_schema_cache: dict[str, set[str]]` and `get_engine_schema(engine: str)` to `ai-wrapper/main.py`.
  - Inspects image digest (`sha256:...`) via `podman image inspect` and performs CPU-only container inspection `podman run --rm --entrypoint /bin/cat`.
- [x] **Task 2.3: Implement 3-Layer Validator & Fast 503 Rejection**
  - Extended `_validate_args` and `acquire_engine` in `ai-wrapper/main.py`.
  - Validates `[args]` against base allowlist, per-engine schema, and regex valve (`_EXTRA_FLAG_RE`/`_EXTRA_BARE_RE`).
  - Raises fast HTTP 503 rejection before triggering engine swap/container stop.
- [x] **Task 3.1: Complete Pytest Suite Verification**
  - Executed full pytest suite against `ai-wrapper/tests/`. All 9 tests passed cleanly.
- [x] **Task 3.2: Budget & Security Audit Verification**
  - Line count delta audited via `git diff --stat`. Total change count is ~350 lines across scripts, Dockerfiles, main logic, and tests.

---

## 2. Automated Test Execution Evidence

**Test Suite Command**: `pytest ai-wrapper/tests/`  
**Execution Environment**: Python 3.14.6 / pytest 9.1.1  
**Result**: 9 passed, 0 failed (0.92s)

### Detailed Test Results

| Test File | Test Case | Status | Summary |
|---|---|---|---|
| `ai-wrapper/tests/test_extract_flags.py` | `test_extract_flags_valid_sample` | PASSED | Verifies extraction of standard flags into sorted unique set |
| `ai-wrapper/tests/test_extract_flags.py` | `test_extract_flags_low_count` | PASSED | Verifies loud failure (`ValueError`) when flag count < 10 |
| `ai-wrapper/tests/test_extract_flags.py` | `test_extract_flags_empty` | PASSED | Verifies loud failure (`ValueError`) on empty input |
| `ai-wrapper/tests/test_extract_flags.py` | `test_cli_execution` | PASSED | Verifies CLI script invocation with stdin and TOML writing |
| `ai-wrapper/tests/test_flags.py` | `test_vram_isolation` | PASSED | Verifies CPU-only inspection omits `--device nvidia.com/gpu=all` |
| `ai-wrapper/tests/test_flags.py` | `test_digest_keyed_cache` | PASSED | Verifies schema caching by `sha256:...` digest |
| `ai-wrapper/tests/test_flags.py` | `test_security_validation` | PASSED | Verifies rejection of command injection and path traversal |
| `ai-wrapper/tests/test_flags.py` | `test_three_layer_validation_and_503` | PASSED | Verifies fast HTTP 503 rejection prior to engine swap |
| `ai-wrapper/tests/test_flags.py` | `test_legacy_fallback` | PASSED | Verifies fallback to base allowlist for images without schema |

---

## 3. Design & Threat Matrix Compliance

| Architectural Requirement | Implementation Status | Verification Method |
|---|---|---|
| **Build-time Extraction** | Implemented in `llama-atomic/Dockerfile` and `llama-cuda/Dockerfile` via `nuc-infra/scripts/extract_flags.py`. Exits 1 if < 10 flags. | Tested via `test_extract_flags.py` CLI & unit tests. |
| **CUDA Driver Stubs** | Builder stage uses `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs` to run `--help` without GPU. | Inspected Dockerfiles; verified build step flags. |
| **VRAM Isolation Policy** | `get_engine_schema` runs `podman run --rm --entrypoint /bin/cat` without `--device nvidia.com/gpu=all`. | Confirmed via `test_vram_isolation`. |
| **Digest-Keyed Cache** | `_schema_cache` keys on `sha256:...` image digest to avoid tag mutability issues (`:latest`). | Confirmed via `test_digest_keyed_cache`. |
| **3-Layer Validator** | 1. Base allowlist keys (`ctx_size`, `n_gpu_layers`...). 2. Per-engine schema lookup. 3. Passthrough regex (`_EXTRA_FLAG_RE`/`_EXTRA_BARE_RE`). | Confirmed via `test_security_validation` & `test_three_layer_validation_and_503`. |
| **Fast 503 Rejection** | Validation in `acquire_engine` raises `HTTPException(503)` before issuing `stop_engine()`. | Confirmed via `test_three_layer_validation_and_503` (mocked `stop_engine` not called). |
| **Legacy Fallback** | Missing `/etc/llama-engine/flags.toml` logs warning and falls back to base allowlist validation. | Confirmed via `test_legacy_fallback`. |

---

## 4. Code & Line Budget Summary

- `ai-wrapper/main.py`: 57 insertions, 1 deletion
- `llama-atomic/Dockerfile`: 10 insertions, 4 deletions
- `llama-cuda/Dockerfile`: 8 insertions, 2 deletions
- `nuc-infra/scripts/extract_flags.py`: 60 lines
- `ai-wrapper/tests/`: 218 lines (across `test_extract_flags.py` and `test_flags.py`)
- **Total Changes**: ~350 lines across code and tests (within ~250-350 estimate, <400 line budget).

---

## 5. Conclusion & Final Verdict

The implementation for `per-engine-flag-schema` fulfills all proposal, design, and task specifications. Unit and integration tests pass with 100% success rate, security threat mitigations are verified, and VRAM isolation rules are strictly preserved.

**Final Verdict**: `PASS`
