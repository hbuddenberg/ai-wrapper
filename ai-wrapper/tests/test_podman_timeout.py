import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


@pytest.fixture(autouse=True)
def clear_state():
    main._schema_cache.clear()
    main.active_alias = None
    main._swapping = False
    main._inflight = 0


class _HangingProc:
    """Fake subprocess whose communicate() never returns."""

    def __init__(self, rc_on_kill: int = -9):
        self.returncode = None
        self.killed = False
        self.waited = False
        self._rc_on_kill = rc_on_kill

    async def communicate(self):
        await asyncio.sleep(9999)

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        self.returncode = self._rc_on_kill
        return self.returncode


@pytest.mark.asyncio
async def test_podman_exec_timeout_raises():
    """A hanging podman subprocess raises PodmanTimeout and its child is killed+reaped."""
    fake_proc = _HangingProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        with pytest.raises(main.PodmanTimeout):
            await main._podman_exec("stop", "foo", timeout=0.05)

    assert fake_proc.killed is True
    assert fake_proc.waited is True


class _HangingProcBadCleanup(_HangingProc):
    """Fake subprocess whose kill()/wait() raise unexpected errors during cleanup."""

    def kill(self):
        raise OSError("simulated kill failure")

    async def wait(self):
        raise OSError("simulated wait failure")


@pytest.mark.asyncio
async def test_kill_and_reap_failure_does_not_mask_podman_timeout():
    """If proc.kill()/proc.wait() themselves fail during cleanup, PodmanTimeout
    must still be raised (not replaced by the cleanup error), so callers keep
    getting the intended 503 instead of an unmapped 500."""
    fake_proc = _HangingProcBadCleanup()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        with pytest.raises(main.PodmanTimeout):
            await main._podman_exec("stop", "foo", timeout=0.05)


@pytest.mark.asyncio
async def test_podman_check_false_still_raises_on_timeout():
    """A timeout must raise PodmanTimeout even when check=False, and must be
    distinguishable from a clean non-zero exit (which check=False tolerates)."""
    fake_proc = _HangingProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        with pytest.raises(main.PodmanTimeout):
            await main.podman("stop", "--ignore", "-t", "10", "foo",
                               check=False, timeout=0.05)

    # Contrast: a clean non-zero exit with check=False must NOT raise at all.
    class _CleanFailProc:
        returncode = 1

        async def communicate(self):
            return b"", b"no such container"

    async def fake_clean_fail(*args, **kwargs):
        return _CleanFailProc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_clean_fail):
        out = await main.podman("stop", "--ignore", "-t", "10", "foo", check=False)
        assert out == ""


@pytest.mark.asyncio
async def test_podman_rc_returns_int_without_timeout():
    """podman_rc() returns the bare exit code (int), unlike podman()'s str output."""

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        rc = await main.podman_rc("container", "exists", "llama-engine")

    assert rc == 0
    assert isinstance(rc, int)

    class _FakeProcNonZero:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_nonzero(*args, **kwargs):
        return _FakeProcNonZero()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_nonzero):
        rc2 = await main.podman_rc("container", "exists", "llama-engine")

    assert rc2 == 1


@pytest.mark.parametrize(
    "raw,default,expected,should_warn",
    [
        ("abc", 30.0, 30.0, True),       # not a number -> default + warn
        ("0", 30.0, 30.0, True),         # non-positive -> default + warn
        ("-5", 30.0, 30.0, True),        # negative -> default + warn
        ("", 30.0, 30.0, False),         # unset/empty -> default, no warn (not operator error)
        ("5", 30.0, 5.0, True),          # valid but <=10 -> used, but warns (below stop -t 10 grace)
        ("50", 30.0, 50.0, False),       # valid, no warn
    ],
)
def test_env_bound_parsing_table(caplog, raw, default, expected, should_warn):
    """_parse_podman_timeout falls back to default + warns on invalid/non-positive
    input, and warns (but still uses the value) when <=10s (near the stop -t 10 grace)."""
    caplog.set_level(logging.WARNING, logger="ai-wrapper")
    with patch.dict("os.environ", {"PODMAN_TIMEOUT_S": raw}):
        result = main._parse_podman_timeout("PODMAN_TIMEOUT_S", default)

    assert result == expected
    if should_warn:
        assert any("PODMAN_TIMEOUT_S" in r.message for r in caplog.records)
    else:
        assert not any("PODMAN_TIMEOUT_S" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_stop_engine_uses_bounded_podman_rc():
    """stop_engine()'s 'container exists' verification must call podman_rc(),
    not spawn its own raw asyncio.create_subprocess_exec."""
    with patch.object(main, "podman", new_callable=AsyncMock) as mock_podman, \
         patch.object(main, "podman_rc", new_callable=AsyncMock) as mock_podman_rc, \
         patch("asyncio.create_subprocess_exec") as mock_raw_exec:
        mock_podman_rc.return_value = 1  # container gone -> success
        await main.stop_engine()

    mock_podman_rc.assert_awaited_once_with("container", "exists", main.ENGINE_CONTAINER)
    mock_raw_exec.assert_not_called()


@pytest.mark.asyncio
async def test_stop_engine_clears_active_alias_in_finally_on_failure():
    """Even when stop_engine() raises ('still exists'), active_alias must be
    cleared afterward (finally block) — an orphan container must not leave
    the wrapper believing the old alias is still live."""
    main.active_alias = "some-model"

    with patch.object(main, "podman", new_callable=AsyncMock), \
         patch.object(main, "podman_rc", new_callable=AsyncMock) as mock_podman_rc:
        mock_podman_rc.return_value = 0  # container STILL exists -> failure path
        with pytest.raises(RuntimeError, match="still exists"):
            await main.stop_engine()

    assert main.active_alias is None


@pytest.mark.asyncio
async def test_same_alias_request_retries_swap_after_failed_stop():
    """After a failed stop_engine() clears active_alias, a same-alias
    acquire_engine() call must NOT short-circuit at the active_alias==alias
    check — it must fall through and call start_engine() again."""
    entry = {
        "alias": "model-a",
        "engine": "llama-cuda",
        "folder": "model-a",
        "file": "model.gguf",
        "args": {},
    }

    # Simulate a previously failed stop: active_alias was cleared by the
    # finally block even though the stop itself raised.
    with patch.object(main, "podman", new_callable=AsyncMock), \
         patch.object(main, "podman_rc", new_callable=AsyncMock) as mock_podman_rc:
        mock_podman_rc.return_value = 0  # still exists -> stop_engine raises
        with pytest.raises(RuntimeError):
            await main.stop_engine()
    assert main.active_alias is None

    with patch.object(main, "get_engine_schema", new_callable=AsyncMock, return_value=None), \
         patch.object(main, "start_engine", new_callable=AsyncMock) as mock_start:
        await main.acquire_engine("model-a", entry)

    # It must NOT have short-circuited: start_engine() must have been called
    # to retry the swap, because active_alias no longer equals "model-a".
    mock_start.assert_awaited_once_with(entry)
    assert main.active_alias == "model-a"
    assert main._inflight == 1


@pytest.mark.asyncio
async def test_lifespan_network_probe_bounded_and_warns_on_timeout(caplog):
    """lifespan()'s startup 'network exists' probe must use the bounded
    podman_rc() path; a stall must be caught and logged as a warning, and
    must NOT block application startup."""
    caplog.set_level(logging.WARNING, logger="ai-wrapper")

    with patch.object(main, "podman_rc", new_callable=AsyncMock) as mock_podman_rc, \
         patch.object(main, "API_KEY", "dummy-key"), \
         patch.object(main, "scan_registry", return_value={}), \
         patch.object(main, "stop_engine", new_callable=AsyncMock):
        mock_podman_rc.side_effect = main.PodmanTimeout("network exists timed out")

        async with main.lifespan(main.app):
            pass

    mock_podman_rc.assert_awaited_once_with("network", "exists", main.ENGINE_NETWORK)
    assert any("network" in r.message.lower() and "timed out" in r.message.lower()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_swap_timeout_returns_503_and_clears_swapping():
    """A podman timeout during start_engine() on the swap path must map to
    HTTP 503 and reset _swapping so the guard is released for the next
    request — which must then proceed without hanging."""
    entry = {"alias": "model-b", "engine": "llama-cuda", "folder": "model-b",
              "file": "model.gguf", "args": {}}

    async def raise_timeout(_entry):
        raise main.PodmanTimeout("podman run timed out")

    with patch.object(main, "get_engine_schema", new_callable=AsyncMock, return_value=None), \
         patch.object(main, "start_engine", side_effect=raise_timeout):
        with pytest.raises(HTTPException) as exc_info:
            await main.acquire_engine("model-b", entry)

    assert exc_info.value.status_code == 503
    assert main._swapping is False

    # A subsequent request must not be blocked on _guard.wait_for(...) — it
    # proceeds and retries the swap normally.
    with patch.object(main, "get_engine_schema", new_callable=AsyncMock, return_value=None), \
         patch.object(main, "start_engine", new_callable=AsyncMock) as mock_start2:
        await asyncio.wait_for(main.acquire_engine("model-b", entry), timeout=2)

    mock_start2.assert_awaited_once()
    assert main.active_alias == "model-b"


@pytest.mark.asyncio
async def test_unload_engine_swap_timeout_returns_503():
    """A podman timeout during /v1/unload's stop_engine() call must also map
    to HTTP 503 and reset _swapping."""
    main.active_alias = "model-c"

    async def raise_timeout():
        raise main.PodmanTimeout("podman stop timed out")

    class _FakeRequest:
        headers: dict = {}

    with patch.object(main, "check_auth", return_value=None), \
         patch.object(main, "stop_engine", side_effect=raise_timeout):
        with pytest.raises(HTTPException) as exc_info:
            await main.unload_engine(_FakeRequest())

    assert exc_info.value.status_code == 503
    assert main._swapping is False


@pytest.mark.asyncio
async def test_schema_probe_timeout_degrades_to_legacy_allowlist(caplog):
    """A stalled get_engine_schema() podman call must degrade to the legacy
    allowlist (schema=None) with a warning, AND the 'run' probe call must use
    the SHORT PODMAN_TIMEOUT_S explicitly — this probe runs on every
    acquire_engine(), so it must not silently inherit the long
    PODMAN_RUN_TIMEOUT_S that _podman_exec auto-selects for 'run' args."""
    caplog.set_level(logging.WARNING, logger="ai-wrapper")
    calls = []

    async def spy_then_stall(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "image":
            return "sha256:deadbeef"
        raise main.PodmanTimeout("podman run timed out")

    with patch.object(main, "podman", side_effect=spy_then_stall):
        schema = await main.get_engine_schema("llama-atomic")

    assert schema is None
    assert any("legacy allowlist" in r.message or "schema" in r.message.lower()
               for r in caplog.records)

    run_call = next(c for c in calls if c[0][0] == "run")
    assert run_call[1].get("timeout") == main.PODMAN_TIMEOUT_S
