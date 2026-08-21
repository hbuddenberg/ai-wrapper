# Tasks: Bound ai-wrapper Hang Vectors

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~155 (main.py) + ~310 (2 new test files) + ~6 (env.example) ≈ 470 total |
| 400-line budget risk | Medium |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 (engine-swap-resilience, ~310 lines) → PR 2 (generation-limits, ~161 lines) |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending — ask user: stacked-to-main or feature-branch-chain |

```text
Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: Medium
```

Single-PR total (~470 lines) exceeds the 400-line budget once real
timeout/injection test coverage is added (mocked-only tests were the prior
pattern; this change requires fake-subprocess and caplog-based RED tests per
the spec's success criteria). Splitting along the two capabilities keeps
each PR comfortably under budget with an independent rollback boundary.

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | `engine-swap-resilience`: bounded podman helper, fold-in, `stop_engine` finally, 503 mapping | PR 1 | `python3 -m pytest ai-wrapper/tests/test_podman_timeout.py -v` | `python3 -m pytest ai-wrapper/tests/ -v` (full regression) | Revert `main.py` podman/stop_engine/lifespan/acquire_engine/unload_engine hunks + delete `test_podman_timeout.py` |
| 2 | `generation-limits`: allowlist, CLI-leak guard, injection logic | PR 2 | `python3 -m pytest ai-wrapper/tests/test_max_tokens.py -v` | `python3 -m pytest ai-wrapper/tests/ -v` (full regression) | Revert `_ARGS_ALLOWLIST`/`build_engine_command`/`chat_completions` hunks + delete `test_max_tokens.py` |

All tasks are in `ai-wrapper/main.py` unless noted. New test file per unit.

## Phase 1: Bounded Podman Foundation (PR 1)

- [x] 1.1 RED `test_podman_timeout.py::test_podman_exec_timeout_raises` — fake proc whose `communicate()` hangs; assert `PodmanTimeout`, `kill()` + `wait()` called (Threat: orphaned child).
- [x] 1.2 GREEN: add `PodmanTimeout(RuntimeError)` and `_podman_exec()` (261-272) — `asyncio.wait_for` around `communicate()`, kill+bounded reap on timeout/`CancelledError`.
- [x] 1.3 RED `test_podman_check_false_still_raises_on_timeout` — timeout raises `PodmanTimeout` even with `check=False`, distinguishable from a clean non-zero exit (Threat: timeout mistaken for clean stop).
- [x] 1.4 GREEN: rewrite `podman()` to delegate to `_podman_exec()`; timeout raises unconditionally regardless of `check=`.
- [x] 1.5 RED `test_podman_rc_returns_int_without_timeout` — new `podman_rc()` helper returns `(rc)` on success.
- [x] 1.6 GREEN: implement `podman_rc()` delegating to `_podman_exec()`.
- [x] 1.7 RED `test_env_bound_parsing_table` (parametrized) — invalid/non-positive `PODMAN_TIMEOUT_S`/`PODMAN_RUN_TIMEOUT_S` fall back to default + warn; warn if `<=10` (Threat: operator-supplied bounds).
- [x] 1.8 GREEN: add `PODMAN_TIMEOUT_S`(30)/`PODMAN_RUN_TIMEOUT_S`(300) parsing + startup warning; `podman run` auto-selects the longer bound.

## Phase 2: Fold Inline Subprocesses + stop_engine finally (PR 1)

- [x] 2.1 RED `test_stop_engine_uses_bounded_podman_rc` — `stop_engine()`'s `container exists` check calls `podman_rc()`, not raw `create_subprocess_exec`.
- [x] 2.2 GREEN: replace inline check in `stop_engine()` (280-283) with `podman_rc("container", "exists", ENGINE_CONTAINER)`.
- [x] 2.3 RED `test_stop_engine_clears_active_alias_in_finally_on_failure` — simulate "Container still exists" `RuntimeError`; assert `active_alias is None` afterward (user-approved fix; Threat: orphan container holding VRAM).
- [x] 2.4 GREEN: move `active_alias = None` into a `finally` block in `stop_engine()` (275-289) so it clears on both success and the raise.
- [x] 2.5 RED `test_same_alias_request_retries_swap_after_failed_stop` — after a failed `stop_engine()`, a same-alias `acquire_engine()` call does NOT short-circuit at line 378; it falls through and calls `start_engine()` again.
- [x] 2.6 GREEN: verify `acquire_engine()` needs no further change (2.4 already routes `active_alias is None` to the swap branch); adjust only if 2.5 fails.
- [x] 2.7 RED `test_lifespan_network_probe_bounded_and_warns_on_timeout` — stalled `network exists` check → `lifespan()` logs a warning, startup continues.
- [x] 2.8 GREEN: replace `lifespan()`'s inline probe (447-450) with `podman_rc(...)` wrapped in `try/except PodmanTimeout: log.warning(...)`.

## Phase 3: Swap-Path 503 Mapping + PR 1 Verification (PR 1)

- [x] 3.1 RED `test_swap_timeout_returns_503_and_clears_swapping` — stalled `podman run` inside `start_engine()`; assert `HTTPException(503)`, `_swapping is False`, a queued second request proceeds.
- [x] 3.2 GREEN: add `except PodmanTimeout` clause in `acquire_engine()` (394-396), before the existing `except BaseException`, mapping to 503.
- [x] 3.3 RED `test_unload_engine_swap_timeout_returns_503` — same pattern for `/v1/unload`.
- [x] 3.4 GREEN: add matching `except PodmanTimeout` clause in `unload_engine()` (546-548).
- [x] 3.5 RED `test_schema_probe_timeout_degrades_to_legacy_allowlist` — stalled `get_engine_schema()` call → warning logged, legacy allowlist still enforced (Threat: degraded flag validation).
- [x] 3.6 GREEN: pass `timeout=PODMAN_TIMEOUT_S` explicitly on both `get_engine_schema()` podman calls; confirm existing `except Exception` degrade path covers `PodmanTimeout`.
- [x] 3.7 Update `nuc-infra/env.example` with `PODMAN_TIMEOUT_S`/`PODMAN_RUN_TIMEOUT_S` + comments.
- [x] 3.8 Run `python3 -m pytest ai-wrapper/tests/ -v` and `python -m py_compile ai-wrapper/main.py` — full regression GREEN.

## Phase 4: Generation-Limits Allowlist & CLI-Leak Guard (PR 2)

- [x] 4.1 RED `test_max_tokens.py::test_default_max_tokens_admitted_by_registry_validation` — `[args].default_max_tokens = 2048` passes `_validate_args()`.
- [x] 4.2 GREEN: add `"default_max_tokens"` to `_ARGS_ALLOWLIST` (73-74); extend `_validate_args()` — must be `int`, not `bool`, `>= 0`.
- [x] 4.3 RED `test_default_max_tokens_never_emitted_to_cli` — `build_engine_command()` output contains no `default_max_tokens`-derived flag/value (Threat: subprocess argument composition).
- [x] 4.4 GREEN: add `_WRAPPER_ONLY_ARGS = {"load_timeout", "default_max_tokens"}` filter as the first line of `build_engine_command()` (234); every branch reads the filtered dict.

## Phase 5: Generation-Limits Injection Logic + PR 2 Verification (PR 2)

- [x] 5.1 RED `test_injection_precedence_table` (parametrized) — per-model `default_max_tokens` > `DEFAULT_MAX_TOKENS` env(4096) > disabled when resolved value is `0`.
- [x] 5.2 GREEN: implement `_inject_default_max_tokens(body, alias, entry)` — omission check (`body.get(k) is not None` for `max_tokens`/`max_completion_tokens`/`n_predict`), precedence resolution, mutate `body["max_tokens"]`, INFO log.
- [x] 5.3 RED `test_explicit_value_preserved_including_null_and_zero` — `max_tokens: 256` untouched; `max_tokens: null` treated as omitted; `max_tokens: 0` treated as explicit (not injected over).
- [x] 5.4 GREEN: confirm/adjust omission predicate for the null/zero edge cases from 5.3.
- [x] 5.5 RED `test_streaming_request_receives_same_injection` — `TestClient` with `stream: true`; body forwarded to `stream_upstream()` carries the injected value.
- [x] 5.6 GREEN: call `_inject_default_max_tokens()` once in `chat_completions()` (675-686), before the stream/non-stream branch.
- [x] 5.7 RED `test_injection_logs_info_line_with_alias_and_value` — caplog contains alias + resolved value.
- [x] 5.8 GREEN: confirm `log.info("Default max_tokens injected: model=%s value=%d source=%s", ...)` format matches; adjust if 5.7 fails.
- [x] 5.9 Update `nuc-infra/env.example` with `DEFAULT_MAX_TOKENS` (default 4096) + comment.
- [x] 5.10 Run `python3 -m pytest ai-wrapper/tests/ -v` and `python -m py_compile ai-wrapper/main.py`; verify all four `proposal.md` Success Criteria against behavior.
