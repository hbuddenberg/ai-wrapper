# Proposal: Bound ai-wrapper Hang Vectors

## Intent
Two independent defects let a single request hold the VRAM Director indefinitely.

**A — unbounded podman subprocess (code-confirmed hazard).** `podman()` (`main.py:261-272`) awaits `proc.communicate()` with no timeout, and `stop_engine()` (280-283) plus `lifespan()` (447-450) spawn their own unbounded subprocesses. `start_engine()` runs *outside* `_guard` after `_swapping=True`; a stalled podman socket there raises nothing, so `_clear_swapping()` never runs and every later request blocks forever on `_guard.wait_for(...)`.

**B — runaway generation (live incident, 2026-08-20).** `gemma-4-12b-it-heretic` (ctx 65536) generated for 30+ min at 80% GPU with no client `max_tokens`; heretic fine-tunes emit EOS unreliably. `httpx.Timeout(600.0)` in `chat_completions()` (696-704) and `stream_upstream()` (707-720) is a *per-read* timeout that resets on every chunk, so it never fires. Resolved manually via `podman restart llama-engine`.

## Scope

### In Scope
- Route every podman invocation through one bounded helper returning `(rc, stdout, stderr)`; kill the child on expiry and raise **regardless of `check=`** (a timeout must never be mistaken for a clean stop).
- Two bounds: short default (`PODMAN_TIMEOUT_S`, 30s) and a longer bound for `podman run` (image pull path).
- Map a swap-path timeout to HTTP 503; confirm the existing `except BaseException: await _clear_swapping()` handlers (394-396, 546-548) reset `_swapping`.
- Inject `max_tokens` server-side in `chat_completions()` before forwarding (both stream and non-stream) when the client sends none.
- Precedence: `[args].default_max_tokens` (per-model, wrapper-only key) > `DEFAULT_MAX_TOKENS` env (default **4096**) > no injection if set to `0`.
- Log an INFO line whenever the default is injected (alias, resolved value) so a truncated-looking answer is traceable to the default rather than a model failure.

### Out of Scope
- Total-stream-duration timeout (user chose injection only).
- `WRAPPER_MODE`/persistent logic, `KEYS_FILE`/auth, registry scanning.
- Engine images, Dockerfiles, compose changes.

## Capabilities

### New Capabilities
- `engine-swap-resilience`: bounded podman I/O and guaranteed swap-guard recovery.
- `generation-limits`: server-side default generation cap on forwarded requests.

### Modified Capabilities
None.

## Approach
Injection treats `max_tokens`, `max_completion_tokens`, and `n_predict` as already-set; explicit `null` counts as omitted. `default_max_tokens` joins `_ARGS_ALLOWLIST` but is never emitted by `build_engine_command()` — it is a wrapper directive, not a `llama-server` flag. `get_engine_schema()` already degrades to the legacy allowlist on exception, so a timeout there is non-fatal.

## Affected Areas
| Area | Impact | Change |
|------|--------|--------|
| `ai-wrapper/main.py:261-289` | Modified | Bounded podman helper; fold inline subprocesses in |
| `ai-wrapper/main.py:447-450` | Modified | Startup `network exists` via helper |
| `ai-wrapper/main.py:96-125`, `234-258` | Modified | Allowlist `default_max_tokens`, keep out of CLI |
| `ai-wrapper/main.py:675-720` | Modified | Body injection before forwarding |
| `ai-wrapper/tests/` | New | Timeout + injection coverage (podman is fully mocked today) |
| `nuc-infra/env.example` | Modified | Document new env vars |

## Risks
| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Bound too tight aborts a legitimate slow swap | Med | Bound only the subprocess, not the health poll; longer bound for `run`; env-tunable |
| 4096 truncates long legitimate answers | Med | Per-model override; `0` disables |
| Injection breaks a client that relies on unlimited output | Low | Only fills an absent field; never overwrites |
| Timeout leaves an orphan `llama-engine` holding VRAM | Med | Next `acquire_engine()` calls `stop_engine()`; startup cleanup already exists |

## Rollback Plan
Revert `ai-wrapper/main.py` and redeploy `ghcr.io/<owner>/ai-wrapper:latest`. Both fixes are additive and default-on; setting `DEFAULT_MAX_TOKENS=0` and a very large `PODMAN_TIMEOUT_S` restores current behaviour without a rollback.

## Success Criteria
- [ ] No `podman` invocation in `main.py` can await unbounded.
- [ ] A simulated podman stall raises, clears `_swapping`, returns 503, and the next request succeeds.
- [ ] A request without `max_tokens` reaches the engine with the resolved default; one with any explicit value is untouched.
- [ ] Per-model `[args].default_max_tokens` overrides the env; it never appears in the `llama-server` command line.

## Resolved Decisions (user, 2026-08-20)
1. Default is **4096 global**; per-model `[args].default_max_tokens` override stays available for individual heretic/no-reliable-EOS models if 4096 proves insufficient for a specific case — not pre-set for `gemma-4-12b-it-heretic` at proposal time.
2. Injection **is logged** (INFO, alias + resolved value) — see Scope.
3. Podman timeout on swap → **503, next request retries the swap** (no forced immediate `stop_engine()` on timeout itself; the existing recovery path already re-invokes `stop_engine()` at the start of the next `start_engine()` call).

## Known Accepted Gap
`max_tokens` caps a runaway generation's *length*, not its *duration*. A model producing 4096 tokens at 3 tok/s still holds the GPU for ~23 minutes. The user explicitly declined a total-stream-duration timeout (Out of Scope) in favor of the simpler, OpenAI-contract-compatible injection — this residual slow-generation exposure is accepted, not an oversight.
