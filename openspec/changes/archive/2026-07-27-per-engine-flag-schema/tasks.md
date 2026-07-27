# Tasks: Per-Engine Flag Schema

## Review Workload Forecast

```text
Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Low
```

Estimated total lines: ~250–350 lines across `nuc-infra/scripts/extract_flags.py`, `llama-atomic/Dockerfile`, `llama-cuda/Dockerfile`, `ai-wrapper/main.py`, and `ai-wrapper/tests/test_flags.py`.

---

## Task Breakdown

### Phase 1: Build-Time Extraction Script & Dockerfile Integrations

- [x] **Task 1.1: Build-Time Flag Extraction Script & RED Unit Test**
  - **Description**: Create `nuc-infra/scripts/extract_flags.py` to parse `llama-server --help` output into TOML `[flags]` format (`flags = ["--ctx-size", "-c", ...]`). Include loud failure (exit code 1) on invalid format or low flag count (<10 flags). Create unit test `ai-wrapper/tests/test_extract_flags.py`.
  - **Threat Matrix Mitigation**: Addresses *Malformed Help Extraction* threat by guaranteeing loud failure during Docker build if help output is corrupted or unexpectedly structured.
  - **Focused test command**: `pytest ai-wrapper/tests/test_extract_flags.py`
  - **Runtime harness**: `pytest`
  - **Rollback boundary**: Delete `nuc-infra/scripts/extract_flags.py` and `ai-wrapper/tests/test_extract_flags.py`.
  - **Steps**:
    1. Write RED unit test in `ai-wrapper/tests/test_extract_flags.py` expecting non-zero exit code on malformed `--help` text, empty text, or output yielding <10 flags.
    2. Implement `nuc-infra/scripts/extract_flags.py` with regex flag matching for `-s, --long` and `--long` flags, generating `/etc/llama-engine/flags.toml`.
    3. Run test command to confirm GREEN status.

- [x] **Task 1.2: Dockerfile Builder Capture Integration**
  - **Description**: Update `llama-atomic/Dockerfile` and `llama-cuda/Dockerfile` builder stages to execute `llama-server --help` with `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs` and parse via `extract_flags.py`. Copy `/etc/llama-engine/flags.toml` into runtime image.
  - **Focused test command**: `podman build -t llama-atomic:test -f llama-atomic/Dockerfile .`
  - **Runtime harness**: `podman`
  - **Rollback boundary**: Revert `llama-atomic/Dockerfile` and `llama-cuda/Dockerfile` to previous commit.
  - **Steps**:
    1. Update `llama-atomic/Dockerfile` builder stage to install `python3` (if absent), run `LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs /src/build/bin/llama-server --help 2>/dev/null | python3 nuc-infra/scripts/extract_flags.py /etc/llama-engine/flags.toml`, and update outdated comment at lines 39-40.
    2. Copy `/etc/llama-engine/flags.toml` to Stage 2 runtime image in `llama-atomic/Dockerfile`.
    3. Apply identical builder extraction and Stage 2 `COPY` changes to `llama-cuda/Dockerfile`.
    4. Ensure `SOURCE_REF`, `GGML_CUDA`, `CMAKE_CUDA_ARCHITECTURES`, and `GGML_NATIVE` build args remain byte-identical.

---

### Phase 2: `ai-wrapper` Lazy Digest-Cached Discovery & 3-Layer Validator

- [x] **Task 2.1: RED Unit & Integration Test Suite (`ai-wrapper/tests/test_flags.py`)**
  - **Description**: Add unit and integration tests covering CPU-only inspection, digest-keyed schema caching, 3-layer argument validation, command injection/path traversal security, fast HTTP 503 rejection, and legacy fallback.
  - **Threat Matrix Mitigations**:
    - *VRAM Isolation Leak*: Verify container inspection omits `--device nvidia.com/gpu=all`.
    - *Tag Poisoning / Stale Cache*: Verify schema cache keys on image digest `sha256:...`.
    - *Command Injection & Path Traversal*: Verify strict regex validation on `extra` flags and path sanitization.
  - **Focused test command**: `pytest ai-wrapper/tests/test_flags.py`
  - **Runtime harness**: `pytest`
  - **Rollback boundary**: Remove `ai-wrapper/tests/test_flags.py`.
  - **Steps**:
    1. Write RED test `test_vram_isolation`: assert schema cat execution uses `podman run --rm --entrypoint /bin/cat` WITHOUT `--device nvidia.com/gpu=all`.
    2. Write RED test `test_digest_keyed_cache`: assert cache uses image digest rather than image tag.
    3. Write RED test `test_security_validation`: assert command injection payloads and path traversal attempts in `extra`/model paths are rejected.
    4. Write RED test `test_three_layer_validation_and_503`: assert unknown extra flags trigger fast HTTP 503 rejection before engine swap.
    5. Write RED test `test_legacy_fallback`: assert missing `flags.toml` falls back to base allowlist with warning log.

- [x] **Task 2.2: Implement `get_engine_schema` and Lazy Digest Caching**
  - **Description**: Add `get_engine_schema(engine: str)` and digest caching dictionary `_schema_cache` to `ai-wrapper/main.py`.
  - **Focused test command**: `pytest ai-wrapper/tests/test_flags.py -k test_digest_keyed_cache`
  - **Runtime harness**: `pytest`
  - **Rollback boundary**: Remove `get_engine_schema` and `_schema_cache` from `ai-wrapper/main.py`.
  - **Steps**:
    1. Define `_schema_cache: dict[str, set[str]] = {}` in `ai-wrapper/main.py`.
    2. Implement `get_engine_schema` to inspect container image digest via `podman image inspect`.
    3. On cache hit, return cached flag set. On cache miss, execute CPU-only container inspection `podman run --rm --entrypoint /bin/cat <image> /etc/llama-engine/flags.toml`, parse TOML flags, and store in cache by digest.
    4. Handle missing `/etc/llama-engine/flags.toml` by returning `None` and logging a warning.

- [x] **Task 2.3: Implement 3-Layer Validator & Fast 503 Rejection**
  - **Description**: Extend `_validate_args` and `acquire_engine` in `ai-wrapper/main.py` to perform 3-layer validation against base allowlist, per-engine schema, and `extra` regex valve. Fail fast with HTTP 503 on unknown flags before stopping the active engine.
  - **Focused test command**: `pytest ai-wrapper/tests/test_flags.py -k "test_three_layer or test_security"`
  - **Runtime harness**: `pytest`
  - **Rollback boundary**: Revert `_validate_args` and `acquire_engine` in `ai-wrapper/main.py`.
  - **Steps**:
    1. Extend `_validate_args` to accept engine schema flag set.
    2. Validate `[args]` keys against base allowlist (`ctx_size`, `n_gpu_layers`, `flash_attn`, `draft_model`, `draft_max`, `draft_min`, `load_timeout`, `extra`).
    3. Validate flags in `extra` list against per-engine schema (if present) and `_EXTRA_FLAG_RE`/`_EXTRA_BARE_RE` regexes.
    4. In `acquire_engine`, trigger `get_engine_schema` and validate target model `[args]` prior to issuing container stop commands. Raise HTTP 503 if validation fails.

---

### Phase 3: Verification & Integration Testing

- [x] **Task 3.1: Complete Pytest Suite Verification**
  - **Description**: Execute full pytest suite to verify parser, discovery, validation, caching, 503 rejection, and legacy fallback end-to-end.
  - **Focused test command**: `pytest ai-wrapper/tests/`
  - **Runtime harness**: `pytest`
  - **Rollback boundary**: Revert all modified code files.
  - **Steps**:
    1. Execute `pytest ai-wrapper/tests/`.
    2. Confirm 100% test pass rate for all RED/GREEN test cases.

- [x] **Task 3.2: Budget & Security Audit Verification**
  - **Description**: Perform static code check to verify line count (<400 lines), verify Threat Matrix mitigations, and validate rollback plan readiness.
  - **Focused test command**: `git diff --stat`
  - **Runtime harness**: `bash`
  - **Rollback boundary**: N/A
  - **Steps**:
    1. Run `git diff --stat` to verify total change size remains within the ~250–350 line estimate.
    2. Confirm all threat matrix mitigations are verified by active tests.
