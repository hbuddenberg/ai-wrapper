# engine-swap-resilience Specification

## Purpose

Bounds every podman subprocess invocation in `ai-wrapper/main.py` with a timeout, guaranteeing a stalled podman CLI/socket can never leave the swap guard (`_swapping`) stuck forever. A timeout is always a raised error, never conflated with a clean non-zero exit (`check=False`).

## Requirements

### Requirement: Bounded Podman Subprocess Execution

Every podman invocation MUST be routed through the shared `podman()` helper (or an equivalent bounded call) and bounded by an `asyncio.wait_for`-style timeout. This applies to the shared `podman()` helper itself, `stop_engine()`'s inline `podman container exists` verification, and `lifespan()`'s startup `podman network exists` check — no podman subprocess may await `proc.communicate()` unbounded.

The default bound MUST be configurable via `PODMAN_TIMEOUT_S` (30s default). `podman run` (image pull path) MUST use a longer, separately configurable bound.

#### Scenario: Normal podman call completes within bound

- GIVEN a `podman()` call with the default 30s timeout
- WHEN the podman CLI returns within the bound
- THEN the call returns `(rc, stdout, stderr)` exactly as today, unaffected by the added bound

#### Scenario: podman run uses the longer image-pull bound

- GIVEN `start_engine()` invokes `podman run` for an image not yet cached locally
- WHEN the pull takes longer than the short default bound but less than the `run`-specific bound
- THEN the call still succeeds — the short 30s bound is not applied to `podman run`

### Requirement: Timeout Always Raises

A podman subprocess timing out MUST always raise an exception, regardless of the `check=` parameter passed to the helper. A timeout MUST NOT be treated as, or conflated with, a clean `check=False` non-zero exit.

#### Scenario: Timeout with check=False still raises

- GIVEN a call to `podman(..., check=False)` where the caller normally tolerates non-zero exit codes
- WHEN the underlying podman process exceeds the timeout bound
- THEN the helper raises a timeout-specific exception rather than returning a non-zero-rc result

#### Scenario: Timeout with check=True raises the timeout, not a generic RuntimeError

- GIVEN a call to `podman(..., check=True)`
- WHEN the process hangs past the bound
- THEN the raised exception is distinguishable as a timeout, not the existing `RuntimeError` used for a clean non-zero exit

### Requirement: Startup and Cleanup Checks Are Bounded

`stop_engine()`'s inline `podman container exists` verification (main.py:280-283) and `lifespan()`'s startup `podman network exists` check (main.py:447-450) MUST use the same bounded execution path as the shared `podman()` helper — they MUST NOT retain their own unbounded `asyncio.create_subprocess_exec` + `communicate()` calls.

#### Scenario: Startup network check times out without blocking app boot

- GIVEN the podman socket is unresponsive when `lifespan()` runs its `network exists` check
- WHEN the bounded check times out
- THEN `lifespan()` logs the timeout as a warning (matching existing non-zero-exit handling) AND application startup continues, exactly as it does today for a non-zero `network exists` exit

### Requirement: Swap-Path Timeout Maps to 503 and Recovers the Guard

A podman timeout occurring on the swap path (inside `acquire_engine()` → `start_engine()`) MUST result in an HTTP 503 response to the triggering request, and the existing `except BaseException: await _clear_swapping()` handlers (main.py:394-396, 546-548) MUST reset `_swapping` to `False` so the guard is released. No new recovery machinery is required beyond confirming these existing handlers fire for the new timeout exception type.

#### Scenario: Podman timeout during swap returns 503 and unblocks the next request

- GIVEN a client request triggers a model swap and the podman CLI stalls past the timeout bound during `start_engine()`
- WHEN the bounded call raises a timeout exception
- THEN the triggering request receives HTTP 503
- AND `_clear_swapping()` runs (via the existing `except BaseException` handler), resetting `_swapping` to `False`
- AND a subsequent request is NOT blocked on `_guard.wait_for(lambda: not _swapping)` — it proceeds and retries the swap normally (via `start_engine()`'s existing `stop_engine()` call at its start)

#### Scenario: No forced immediate stop_engine() on timeout itself

- GIVEN a swap-path podman timeout has occurred and `_swapping` has been cleared
- WHEN the next request calls `acquire_engine()`
- THEN no special forced-cleanup path runs beyond the guard reset — the existing `start_engine()` → `stop_engine()` sequence at the start of the next swap attempt is the sole recovery mechanism

## Non-Goals

- No new orphan-container detection or forced kill beyond the existing `start_engine()`-calls-`stop_engine()`-first pattern.
- No change to `_guard`/`_swapping`/`_inflight` synchronization primitives themselves — only to what happens when a bounded podman call times out.
- No total-request-duration timeout; this capability bounds podman subprocess I/O only, not generation duration (see `generation-limits` for the related, deliberately narrower fix).
