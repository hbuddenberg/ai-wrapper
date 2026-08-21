# Verification Report: ai-wrapper-swap-hang

**Mode**: full spec-driven verification (proposal + specs + design + tasks all present)
**Verified**: 2026-08-21
**Commits verified**: PR1 `522977b` (engine-swap-resilience), PR2 `9149518` (generation-limits), both on `main`

## Completeness

| Item | Status |
|---|---|
| tasks.md | 34/34 tasks marked `[x]`, all confirmed matching actual code (spot-checked 1.7, 2.3/2.4, 3.6, 4.2, 5.2 directly against main.py) |
| proposal.md Success Criteria (4) | All 4 verified true against code and/or tests |
| specs/engine-swap-resilience/spec.md | 4 requirements, 7 scenarios |
| specs/generation-limits/spec.md | 5 requirements, 10 scenarios + 1 non-goal scenario |
| design.md | Matches implementation exactly — no deviations found |

## Build / Test Evidence (executed live, not trusted from prior reports)

```
$ python3 -m pytest ai-wrapper/tests/ -v
======================= 44 passed, 12 warnings in 0.66s ========================
```
44/44 tests pass (5 test_extract_flags.py + 5 test_flags.py baseline + 16 test_podman_timeout.py + 18 test_max_tokens.py). Matches the claimed 44/44 count exactly.

```
$ python -m py_compile ai-wrapper/main.py
exit code 0 (clean)
```

Code inspection confirms **zero remaining unbounded subprocess calls**: `grep -n "create_subprocess_exec\|\.communicate()" ai-wrapper/main.py` returns exactly one hit pair (main.py:339,343), both inside `_podman_exec()`, both wrapped in `asyncio.wait_for(...)`. No other call site in main.py spawns a raw subprocess.

## Spec Compliance Matrix — engine-swap-resilience

| Requirement | Scenario | Status | Evidence |
|---|---|---|---|
| Bounded Podman Subprocess Execution | Normal call completes within bound | PASS | `test_podman_rc_returns_int_without_timeout`, existing `test_flags.py` suite (unaffected) |
| Bounded Podman Subprocess Execution | `podman run` uses longer image-pull bound | **UNTESTED (code-verified)** | `main.py:337-338` auto-selects `PODMAN_RUN_TIMEOUT_S` when `args[0]=="run"` and no explicit timeout given — logic is correct by direct inspection, but no test asserts the resolved default value for a `run` call without an explicit override. The only "run" test (`test_schema_probe_timeout_degrades_to_legacy_allowlist`) asserts the *opposite* case: that `get_engine_schema()` explicitly overrides this with `timeout=PODMAN_TIMEOUT_S`. |
| Timeout Always Raises | `check=False` still raises | PASS | `test_podman_check_false_still_raises_on_timeout` |
| Timeout Always Raises | `check=True` raises distinguishable timeout, not generic RuntimeError | **UNTESTED (code-verified)** | No test passes `check=True` explicitly through a timeout path. Mechanically low-risk: `PodmanTimeout` is raised inside `_podman_exec()` *before* `podman()`'s `if check and rc != 0:` branch is ever reached (main.py:372-380), so `check`'s value cannot affect whether the timeout raises — the check=False test already exercises the identical code path. |
| Startup/Cleanup Checks Are Bounded | Startup network check times out without blocking boot | PASS | `test_lifespan_network_probe_bounded_and_warns_on_timeout` |
| Swap-Path Timeout → 503 + Guard Recovery | Timeout during swap → 503, `_swapping` cleared, next request proceeds | PASS | `test_swap_timeout_returns_503_and_clears_swapping` (also covers `/v1/unload` via `test_unload_engine_swap_timeout_returns_503`) |
| Swap-Path Timeout → 503 + Guard Recovery | No forced immediate `stop_engine()` on timeout itself | PASS (by inspection) | `acquire_engine()`'s `except PodmanTimeout` handler (main.py:513-516) only calls `_clear_swapping()` + raises `HTTPException(503)` — no extra cleanup call added, matching spec exactly |

Also verified: `stop_engine()`'s inline check now calls `podman_rc("container","exists",...)` (`test_stop_engine_uses_bounded_podman_rc`, asserts raw `create_subprocess_exec` is NOT called), and `active_alias` is cleared in a `finally` block on both success and failure (`test_stop_engine_clears_active_alias_in_finally_on_failure`, `test_same_alias_request_retries_swap_after_failed_stop`).

## Spec Compliance Matrix — generation-limits

| Requirement | Scenario | Status | Evidence |
|---|---|---|---|
| Default Injection When Client Omits a Bound | Omit all three keys → injected | PASS | `test_injection_precedence_table[None-4096-4096]` |
| Default Injection When Client Omits a Bound | Explicit `null` treated as omitted | PASS | `test_explicit_value_preserved_including_null_and_zero[body_overrides1-4096]` |
| Default Injection When Client Omits a Bound | Streaming receives identical injection | PASS | `test_streaming_request_receives_same_injection` (TestClient-level, asserts body forwarded to `stream_upstream()` carries injected value) |
| Injection Never Overwrites Explicit Value | Explicit `max_tokens: 256` preserved | PASS | `test_explicit_value_preserved_including_null_and_zero[body_overrides0-256]` |
| Precedence Order | Per-model override wins over env | PASS | `test_injection_precedence_table[1024-4096-1024]` |
| Precedence Order | Env default applies with no override | PASS | `test_injection_precedence_table[None-4096-4096]` |
| Precedence Order | Resolved value 0 disables injection | PASS | `test_injection_precedence_table[0-4096-None]`, `[None-0-None]` |
| Injection Is Logged | INFO line names alias + resolved value | PASS | `test_injection_logs_info_line_with_alias_and_value` (caplog assertion) |
| Allowlisted but Never Emitted to CLI | Config passes `scan_registry()`/`_validate_args()` | PASS | `test_default_max_tokens_admitted_by_registry_validation`, `test_default_max_tokens_zero_is_valid`, `test_default_max_tokens_rejects_invalid_types_and_negatives` (5-case param table incl. `bool` exclusion) |
| Allowlisted but Never Emitted to CLI | Never appears on `llama-server` command line | PASS | `test_default_max_tokens_never_emitted_to_cli` — asserts BOTH the guard's own existence (`main._WRAPPER_ONLY_ARGS`) and absence from `build_engine_command()` output |
| Non-Goal: long-running active generation not interrupted | (negative scenario, no test required per spec) | CONFIRMED HOLDS | Code-verified: `stream_upstream()`/`chat_completions()` still use only `httpx.Timeout(600.0)` (a per-read timeout, unchanged), no wall-clock/duration cap was added anywhere in the diff |

## Known Accepted Gap — explicitly re-verified

The proposal's "Known Accepted Gap" (`max_tokens` bounds generation **length**, not wall-clock **duration**) is still correctly out of scope:
- No code in `main.py` implements a total-stream-duration timeout. `httpx.Timeout(600.0)` in both `chat_completions()` and `stream_upstream()` is unchanged from before this change and remains a per-read timeout that resets on every chunk (exactly as the proposal describes the pre-existing, deliberately-not-fixed mechanism).
- The `generation-limits` spec's own Non-Goals section states this explicitly as a negative/non-requirement scenario ("wrapper does NOT abort/timeout on duration grounds alone... this is correct behavior per spec, not a bug").
- No test in `test_max_tokens.py` or elsewhere asserts or exercises duration-based cutoff — confirmed no test accidentally claims coverage of duration-bounding.
**Conclusion**: gap is accurately documented and correctly not silently expected to be covered. No action needed.

## Four Documented Non-Blocking Follow-ups — re-verified still open and accurately described

| # | Item | Still open? | Evidence |
|---|---|---|---|
| 1 | `_parse_podman_timeout()`'s warning message hardcodes "`stop -t 10` grace period" phrasing regardless of which env var triggered it | YES | `main.py:72-74` — message reused verbatim for both `PODMAN_TIMEOUT_S` and `PODMAN_RUN_TIMEOUT_S`; misleading for the latter (image-pull bound has no relation to `stop -t 10`) |
| 2 | `_kill_and_reap()`'s reap bound hardcoded to `5` seconds | YES | `main.py:365` — literal `5`, not env-configurable |
| 3 | No comment marking `except PodmanTimeout`/`except BaseException` ordering as load-bearing | YES | `main.py:513-519` (`acquire_engine()`) and `main.py:670-676` (`unload_engine()`) — order is correctness-critical (`PodmanTimeout ⊂ RuntimeError ⊂ BaseException`) but uncommented |
| 4 | `_kill_and_reap()`'s `ProcessLookupError` branch has no dedicated test | YES | `main.py:359-361` — `test_kill_and_reap_failure_does_not_mask_podman_timeout` uses `_HangingProcBadCleanup` which raises `OSError` (hits the generic `except Exception` branch), not `ProcessLookupError` specifically |

All four confirmed still present in the code and accurately described; none were silently fixed or misrepresented.

## Issues

### CRITICAL
None. No spec requirement is unimplemented, no task is falsely marked complete, and no regression was found. `44/44` tests pass live; `py_compile` clean.

### WARNING
1. **Test coverage gap — `podman run` default-bound auto-select** (engine-swap-resilience, Req1/Scenario2): no runtime test directly asserts that a `run` call without an explicit `timeout=` resolves to `PODMAN_RUN_TIMEOUT_S`. Code is correct by direct inspection (one-line ternary, `main.py:337-338`) and is exercised implicitly by every real `start_engine()` call, but per strict spec-verification standards ("a scenario is compliant only when a covering test passed at runtime") this scenario lacks direct coverage. Low actual risk; recommend a small follow-up test (e.g. `test_podman_run_call_auto_selects_run_timeout_by_default`).
2. **Test coverage gap — `check=True` timeout path** (engine-swap-resilience, Req2/Scenario2): no test explicitly passes `check=True` through a timeout. Mechanically proven safe by code reading (the `check` parameter is never consulted before `PodmanTimeout` would already have propagated), so this is a coverage-only gap, not a behavior risk.
3-6. The four already-known non-blocking follow-up items above — reconfirmed open, unchanged, accurately described (not new findings; carried forward from the PR1 review receipt).

### SUGGESTION
None new.

## Design Coherence

design.md's implementation notes for both capability A (bounded podman subprocess) and capability B (max_tokens injection) match the shipped code line-for-line: `_podman_exec`/`PodmanTimeout`/`podman_rc` shape, `finally`-based `active_alias` clear, `except PodmanTimeout` placed before `except BaseException`, `_WRAPPER_ONLY_ARGS` guard-by-construction, single injection point in `chat_completions()` before `acquire_engine()`, `stream_upstream()` requiring zero edits. No deviations found.

## Final Verdict

**PASS WITH WARNINGS**

Rationale: all 34 tasks are genuinely complete and match the code; all functional spec requirements are correctly implemented; the full 44-test suite passes live and `py_compile` is clean; the "Known Accepted Gap" remains correctly and exclusively documented (not silently expected to be covered); the four previously-known follow-up items are unchanged and accurately described. The only findings are two narrow test-coverage gaps on already-correct code (not functional defects) plus the four pre-existing, already-accepted cosmetic follow-ups — none block archiving this change, but the two new coverage gaps are worth a small fast-follow test addition before the next incident review of this area.
