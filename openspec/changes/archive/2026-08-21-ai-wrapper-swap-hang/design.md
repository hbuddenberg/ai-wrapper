# Technical Design: Bound ai-wrapper Hang Vectors

Covers exactly two capabilities: `engine-swap-resilience` (A) and `generation-limits` (B).
The wrapper stays single-file by intent (`openspec/config.yaml: rules.design`).

## A — Bounded Podman Subprocess

### Architecture Decisions

| Decision | Choice | Alternatives rejected | Rationale |
|---|---|---|---|
| Timeout placement | `asyncio.wait_for(proc.communicate(), t)` inside one private `_podman_exec()` | Wrapping whole call sites | `wait_for` cancels the *await*, never the child; the kill/reap must live next to the `Popen` handle. |
| Orphan handling | On expiry **and** on `CancelledError`: `proc.kill()` (suppress `ProcessLookupError`) → `await wait_for(proc.wait(), 5)` → raise | Bare `raise` after timeout | Without an explicit kill, a wedged `podman` client leaks per request; SIGKILL is unblockable so the reap returns promptly. |
| Exception | `class PodmanTimeout(RuntimeError)` | Reuse `asyncio.TimeoutError` | Distinguishable at swap sites for 503 mapping, while `RuntimeError` keeps the existing `except RuntimeError` startup/shutdown cleanup handlers (464, 487) non-fatal. |
| `check=` interaction | Timeout raises **unconditionally**; `check=False` still only suppresses non-zero `rc` | One flag for both | A swallowed timeout in `stop_engine()` would fall through to `active_alias = None` with the engine still holding VRAM. This is the design's central invariant. |
| Return contract | Keep `podman(...) -> str`; add `podman_rc(...) -> int`; both delegate to `_podman_exec() -> (rc, out, err)` | Change `podman()` to a 3-tuple | 4 call sites + existing test mocks (`test_flags.py`) expect `str`; a tuple is churn against the 400-line budget for no gain. |
| Bounds | `PODMAN_TIMEOUT_S=30` default; `PODMAN_RUN_TIMEOUT_S=300` auto-selected when `args[0] == "run"`; per-call `timeout=` override | Single bound | `podman run :latest` may pull a multi-GB CUDA image; `stop -t 10` needs headroom over its own 10s grace. Startup logs the effective bounds and warns if `PODMAN_TIMEOUT_S <= 10`. |
| Schema probe bound | `get_engine_schema()` passes `timeout=PODMAN_TIMEOUT_S` explicitly on both calls | Let its `run` take 300s | It runs on **every** `acquire_engine()` (llama-tom/atomic have no `flags.toml`); a wedged socket would otherwise stall each request 300s. Its `except Exception` already degrades to the legacy allowlist. |
| `stop_engine()` failure state | Move `active_alias = None` into a `finally` so it clears on timeout **and** on the existing "still exists" raise | Leave it after the raise | Resolved decision #3 ("next request retries the swap") only holds if the wrapper stops believing the old alias is live; otherwise a same-alias request short-circuits at line 378 and forwards to a dead engine forever. The code's own comment (286-289) already claims this is the source of truth "including failed swap recovery". |
| Health poll | Unbounded by this change; keeps `load_timeout`/`DEFAULT_LOAD_TIMEOUT_S` | Fold into podman bound | Explicit non-goal — bound the subprocess, not the model load. |

### Inline subprocesses folded in

| Site | Today | After |
|---|---|---|
| `stop_engine()` 280-283 | raw `create_subprocess_exec` `container exists` | `rc = await podman_rc("container", "exists", ENGINE_CONTAINER)` |
| `lifespan()` 447-450 | raw `create_subprocess_exec` `network exists` | `podman_rc(...)` wrapped in `try/except PodmanTimeout: log.warning(...)` — startup stays non-fatal |

### Recovery: no new machinery (confirmed)

`PodmanTimeout` ⊂ `RuntimeError` ⊂ `Exception` ⊂ `BaseException`, so `acquire_engine()` (394-396) and `unload_engine()` (546-548) already catch it and run `_clear_swapping()` (shielded, sets `_swapping=False` + `notify_all()`), waking every `_guard.wait_for(...)` waiter. `_inflight` cannot leak: it is incremented only at 379/400, both *after* the raise point, and `release_engine()` is only reachable from the forward block. The **only** addition is a more specific clause **placed before** the `BaseException` one (order matters — Python matches in source order):

```python
except PodmanTimeout as exc:
    await _clear_swapping()
    log.error("Podman timed out during swap to %r: %s", alias, exc)
    raise HTTPException(503, f"Engine swap timed out for {alias!r}; retry") from exc
except BaseException:
    await _clear_swapping()
    raise
```

### Timeout → client mapping

| Site | Handling | Client sees |
|---|---|---|
| `get_engine_schema` | existing `except Exception` → warn, legacy allowlist | normal 200 |
| `start_engine` (`run`) / its `stop_engine` | new clause above | **503** |
| `/v1/unload` → `stop_engine` | same clause | **503** |
| startup/shutdown cleanup | existing `except RuntimeError` → warn | n/a |

## B — Default `max_tokens` Injection

### Data flow

```
POST /v1/chat/completions
  check_auth → body = await request.json() → 404 if alias not in registry
      │
      ▼  _inject_default_max_tokens(body, alias, entry)   ← SINGLE injection point
      │     any(body.get(k) is not None for k in
      │         ("max_tokens","max_completion_tokens","n_predict")) → return
      │     limit = entry["args"].get("default_max_tokens") ?? DEFAULT_MAX_TOKENS
      │     limit <= 0 → return          else body["max_tokens"] = limit + INFO log
      ▼
  acquire_engine → body.get("stream") ?
        yes → StreamingResponse(stream_upstream(url, body, request))   ← UNCHANGED
        no  → httpx.post(upstream, json=body)
```

`stream_upstream()` (707-720) needs **no edit**: it receives the same already-mutated `dict`. Injecting before `acquire_engine()` keeps the log adjacent to the request and makes it independent of swap outcome.

### Rules

- **Omission** = key absent **or** explicitly `null` (`body.get(k) is not None`). Any non-null value, including `0`, counts as set → no injection.
- **Precedence**: `[args].default_max_tokens` > `DEFAULT_MAX_TOKENS` env (4096) > disabled when the resolved value is `0`.
- **Injected key** is `max_tokens` (the OpenAI-compatible field llama-server's `/v1/chat/completions` accepts); `n_predict` is only read as an already-set marker.
- **Log** (lazy `%` style, matching `log.info("Engine ready: %s (%s)", ...)`):
  `log.info("Default max_tokens injected: model=%s value=%d source=%s", alias, limit, source)` with `source` ∈ `config|env`.

### CLI-leak guard

`default_max_tokens` joins `_ARGS_ALLOWLIST` (73-74) and is validated in `_validate_args`: must be `int`, **not** `bool` (`isinstance(True, int)` is `True`), `>= 0` — else `log.error` + exclude, matching existing validation style.

Guard by construction, not assertion — first line of `build_engine_command()` (234):

```python
_WRAPPER_ONLY_ARGS = {"load_timeout", "default_max_tokens"}   # accepted in config.toml, never a llama-server flag
args = {k: v for k, v in entry["args"].items() if k not in _WRAPPER_ONLY_ARGS}
```

Every branch reads the filtered dict, so a future `if "default_max_tokens" in args:` branch is dead by construction. This also names the pre-existing unnamed convention around `load_timeout`.

## File Changes

| File | Action | Description |
|---|---|---|
| `ai-wrapper/main.py` | Modify | `_podman_exec`/`podman`/`podman_rc`/`PodmanTimeout` (261-272); `stop_engine` fold + `finally` (275-289); `lifespan` network probe (447-450); 503 clauses (394-396, 546-548); allowlist + validation (73-74, 96-125); `_WRAPPER_ONLY_ARGS` filter (234); injection call (675-686) |
| `ai-wrapper/tests/test_podman_timeout.py` | Create | Bounded-helper + swap-recovery coverage |
| `ai-wrapper/tests/test_max_tokens.py` | Create | Precedence, omission, leak-guard coverage |
| `nuc-infra/env.example` | Modify | `PODMAN_TIMEOUT_S`, `PODMAN_RUN_TIMEOUT_S`, `DEFAULT_MAX_TOKENS` |

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | Bounded helper | Patch `asyncio.create_subprocess_exec` with a fake proc whose `communicate()` sleeps forever; assert `PodmanTimeout`, `kill()` called, `wait()` awaited |
| Unit | `check=False` invariant | Same fake with `check=False` → still raises; separate fake with `rc=1` → returns |
| Unit | Injection | Table test over precedence + `{}`, `{"max_tokens": None}`, `{"max_tokens": 0}`, `{"n_predict": 8}` |
| Unit | Leak guard | `assert not any("default_max_tokens" in t or t == "4096" for t in build_engine_command(entry))` |
| Integration | Swap recovery | Stall `podman run` → assert 503, `main._swapping is False`, `active_alias is None`, and a second request proceeds |
| Integration | Stream parity | `TestClient` with `stream: true` → assert forwarded body carries the injected `max_tokens` |
| Static | Whole file | `python -m py_compile ai-wrapper/main.py` (only CI-verifiable check per `openspec/config.yaml`) |

## Threat Matrix

Applicable — the change touches subprocess spawning and process integration.

| Boundary | Applicability | Design response | Planned RED test |
|---|---|---|---|
| Documentation-like paths | N/A — no file classification or execution |—|—|
| Git repository selection | N/A — no VCS automation |—|—|
| Commit state | N/A |—|—|
| Push state | N/A |—|—|
| PR commands | N/A |—|—|
| **Subprocess argument composition** | Applicable | List-form `create_subprocess_exec`, never `shell=True`; `default_max_tokens` filtered out of the command by construction | Assert no wrapper-only key or value in `build_engine_command()` output |
| **Orphaned child on expiry/cancel** | Applicable | Explicit `kill()` + bounded reap in `_podman_exec` on both `TimeoutError` and `CancelledError` | Fake proc records `kill`/`wait` calls |
| **Timeout mistaken for clean stop** | Applicable | Timeout raises regardless of `check=`; `active_alias` cleared in `finally` | `check=False` stall still raises; assert `active_alias is None` |
| **Orphan container holding VRAM** | Applicable | Next `acquire_engine()` → `start_engine()` → `stop_engine()` (`rm -f`) retries; startup cleanup already exists | Second request after a stalled swap issues `stop`/`rm` again |
| **Operator-supplied bounds** | Applicable | Invalid/non-positive env → warn + default; warn if `PODMAN_TIMEOUT_S <= 10` (below `stop -t 10` grace) | Env-parse table test |
| **Degraded flag validation on schema-probe timeout** | Applicable | Pre-existing documented fallback (warn + legacy allowlist); base allowlist and `_EXTRA_*` regexes still apply | Stalled schema probe → warning logged, legacy validation enforced |

## Migration / Rollout

No migration. Both fixes are additive and default-on. `DEFAULT_MAX_TOKENS=0` plus a very large `PODMAN_TIMEOUT_S`/`PODMAN_RUN_TIMEOUT_S` restore current behaviour without redeploying a revert.

## Open Questions

- [ ] Negative schema caching (cache `digest → None` so schema-less engines are probed once, not per request) would cut podman invocations per request from 3 to 1. Deferred — measurable win, but outside the two proposed capabilities.
- [ ] Accepted gap restated: `max_tokens` caps generation *length*, not *duration* (4096 tokens @ 3 tok/s ≈ 23 min of GPU hold). Total-stream-duration timeout explicitly declined by the user.
