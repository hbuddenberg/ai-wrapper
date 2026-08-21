# Exploration: ai-wrapper intermittent hang during/after engine swap

## Current State

`ai-wrapper/main.py` (721 lines). Engine mutual exclusion is a single `asyncio.Condition`
(`_guard`) protecting `active_alias`, `_inflight`, `_swapping`. `acquire_engine()`
(main.py:345-406) sets `_swapping=True`, drains `_inflight` to 0 under the guard,
**releases** the guard, then calls `await start_engine(entry)` OUTSIDE the guard by
design. `start_engine()` (292-342): `stop_engine()` → sleep 1.5s (`VRAM_COOLDOWN_S`)
→ `podman run ...` → health-poll loop bounded by `load_timeout` (default 30s, 2.0s
per-poll httpx timeout). The only paths that reset `_swapping=False` are the happy
path and the `except BaseException: await _clear_swapping()` handlers wrapping
`start_engine()`.

All reads/mutations of `_swapping`/`active_alias`/`_inflight` happen inside
`async with _guard:` with no `await` between check and mutation — a prior-session
hypothesis of a concurrent double-swap race is **refuted** by direct line-by-line
reading; cooperative asyncio scheduling makes it impossible as written.

## Affected Areas

- `ai-wrapper/main.py:261-272` (`podman()` helper) — `asyncio.create_subprocess_exec`
  + `await proc.communicate()` with **no `asyncio.wait_for`/timeout**. Nearly every
  podman interaction funnels through here.
- `ai-wrapper/main.py:275-289` (`stop_engine()`) — calls `podman()` twice, plus its
  own separate unbounded inline subprocess (`podman container exists`, 280-283)
  that bypasses the helper entirely.
- `ai-wrapper/main.py:292-342` (`start_engine()`) — the `CancelledError` handler
  shields a cleanup `stop_engine()` via `asyncio.shield` (line 336); if that inner
  podman call hangs, `asyncio.shield` explicitly blocks outer cancellation from
  reaching it — unrecoverable even under cancellation.
- `ai-wrapper/main.py:345-406` (`acquire_engine()`) — `start_engine()` runs
  **outside** `_guard` after `_swapping=True`; a hang there means `_swapping` never
  flips back, so every queued/future request blocks forever on
  `_guard.wait_for(lambda: not _swapping)`.
- `ai-wrapper/main.py:128-160` (`get_engine_schema()`) — calls `podman()` twice,
  unbounded, on **every** `acquire_engine()` call (confirmed relevant: llama-atomic/
  llama-tom images lack `/etc/llama-engine/flags.toml`), before `_guard` is touched.
- `ai-wrapper/main.py:515-558` (`/v1/unload`) — duplicates the swap/guard pattern;
  same exposure.
- `ai-wrapper/main.py:447-450` (`lifespan()` startup `podman network exists`) — same
  anti-pattern, startup-time only.
- `nuc-infra/podman-compose.yml:79-104` — ai-wrapper bind-mounts the host rootless
  podman.sock (`/run/user/1000/podman/podman.sock`) plain, with
  `security_opt: label=disable`. This single shared socket is the common
  dependency behind every `podman()` call.
- `ai-wrapper/tests/test_flags.py` — all tests mock `main.podman`/`get_engine_schema`
  entirely; zero coverage of real subprocess hang/timeout behavior.

## Root-Cause Hypotheses (ranked by evidence strength)

1. **HIGH (code-confirmed, not live-confirmed)** — Unbounded `podman()` subprocess
   call inside the swap path can permanently deadlock `_swapping=True`. If the
   podman CLI/socket hangs during `start_engine()` (run outside `_guard`), no
   exception is ever raised, `_clear_swapping()` never executes, and every request
   blocks forever on the condition wait. FastAPI/Starlette does not cancel
   non-streaming handlers on client disconnect, so nothing external can unstick it
   either — a true permanent hang matching "colgado."
2. **MEDIUM-HIGH** — Same bug reachable via `get_engine_schema()` on *every*
   request, independent of any swap.
3. **LOW** — Bounded-but-long httpx timeouts on upstream proxy calls; self-resolves.
4. **LOW** — Synchronous `_load_keys()` disk read in `check_auth()`.
5. **REFUTED** — Concurrent double-`_swapping=True` race; code is provably
   race-free as written.

## Live Verification Addendum (orchestrator, same session)

Ran directly against the running system while the user reported it as hung:

- `podman logs ai-wrapper` (full 5427 lines) — every `"Swapping engine: X -> Y"`
  entry has a matching `"Engine ready: ..."` shortly after. No orphaned swap
  (started-but-never-finished) found in the entire log history. Last swap:
  2026-08-19 22:48:54 → ready 22:49:15 (~21s, expected swap latency).
- `podman ps` / `podman inspect ai-wrapper` / `podman inspect llama-engine` — both
  containers `running`, `ai-wrapper` up 12h without restart.
- **Live probe**: `curl http://localhost:5128/v1/models` with a bad bearer token →
  responded in **0.013s** with `401 Unauthorized`. The process is NOT wedged.
- Last 3 log lines are all `GET /v1/models` → `401 Unauthorized`, most recent at
  `2026-08-20 06:14:31`, seconds before the live probe above (also 401). No
  `POST /v1/chat/completions` since `2026-08-20 05:47:34`.

**This contradicts an active occurrence of hypothesis #1/#2 right now.** The
wrapper is fast and responsive; it is correctly rejecting unauthenticated/
mis-keyed requests. The user's "colgado" perception at this moment is more
consistent with a **client-side symptom** (a caller retrying or spinning
indefinitely on repeated 401s, or a misconfigured/expired API key on the client)
than with a wedged `_swapping` guard — though the code-level hazard (#1/#2) is
real, unfixed, and could still cause a genuine hang under different timing (e.g.
during an actual swap, if the podman socket itself stalls).

## Unknowns / Gaps

- **Which client** is issuing the `GET /v1/models` calls that are getting 401s,
  and what key it is sending — needed to confirm the "hang" is really a client-side
  auth-retry loop and not something else. **Needs user input.**
- No occurrence of the code-level hang (#1/#2) found in available log history —
  if it happened, it either self-recovered (contradicts the "permanent" theory) or
  left no trace distinguishable from a normal restart. **Needs the user to
  reproduce and capture `podman logs ai-wrapper` + `podman ps -a` +
  `podman inspect llama-engine` at the exact moment it's stuck next time.**
- Whether the installed `podman` CLI has any client-side default timeout against a
  hung socket that already partially bounds this — not verified.
- Host-level `journalctl`/podman.sock health not accessible from this environment.

## Ready for Proposal

Yes, with a caveat: recommend `sdd-propose` scope BOTH (a) the code-level fix —
bound all `podman()`-family subprocess calls with `asyncio.wait_for()` and define
recovery semantics for a timeout, which is good hardening regardless — and (b)
get the user to confirm/rule out the client-side 401 explanation for *this
specific* incident before treating it as fixed by (a) alone.
