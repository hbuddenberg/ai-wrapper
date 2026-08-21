import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


@pytest.fixture(autouse=True)
def clear_state():
    main._schema_cache.clear()
    main.active_alias = None
    main._swapping = False
    main._inflight = 0


# --- Phase 4: allowlist + CLI-leak guard ---


def test_default_max_tokens_admitted_by_registry_validation():
    """[args].default_max_tokens passes _validate_args instead of being rejected
    as an unknown key."""
    args = {"default_max_tokens": 2048}
    assert main._validate_args(args, "test_config") is True


@pytest.mark.parametrize("bad_value", [-1, True, False, "2048", 3.5])
def test_default_max_tokens_rejects_invalid_types_and_negatives(bad_value):
    """A negative int, bool (isinstance(True, int) is True — must be excluded
    explicitly), string, or float must be rejected, not silently coerced."""
    args = {"default_max_tokens": bad_value}
    assert main._validate_args(args, "test_config") is False


def test_default_max_tokens_zero_is_valid():
    """Zero is a valid resolved value — it means 'disable injection', per the
    precedence spec, not an invalid config value."""
    args = {"default_max_tokens": 0}
    assert main._validate_args(args, "test_config") is True


def test_default_max_tokens_never_emitted_to_cli():
    """build_engine_command() must never emit a default_max_tokens-derived
    flag or value onto the llama-server command line — it is a wrapper-only
    directive consumed for request-body injection, not an engine flag.

    Guard-by-construction: build_engine_command() filters entry["args"]
    through main._WRAPPER_ONLY_ARGS before reading any key, so a future
    branch reading "default_max_tokens" is structurally dead code. This
    asserts the guard's own existence (not just current absence of a
    reading branch), which is what actually prevents a future leak.
    """
    assert "default_max_tokens" in main._WRAPPER_ONLY_ARGS
    assert "load_timeout" in main._WRAPPER_ONLY_ARGS

    entry = {
        "alias": "test-model",
        "engine": "llama-cuda",
        "folder": "test-folder",
        "file": "model.gguf",
        "args": {"default_max_tokens": 2048, "ctx_size": 4096},
    }
    cmd = main.build_engine_command(entry)
    assert "default_max_tokens" not in cmd
    assert "2048" not in cmd
    # Sanity: the co-present real flag IS still emitted (proves filtering is
    # selective, not just an empty args table).
    assert "-c" in cmd
    assert "4096" in cmd


# --- Phase 5: injection logic ---


@pytest.mark.parametrize(
    "per_model_value,env_default,expected_injected",
    [
        (1024, 4096, 1024),  # per-model override wins over env default
        (None, 4096, 4096),  # no per-model key -> env default applies
        (0, 4096, None),  # per-model 0 disables injection even with a nonzero env default
        (None, 0, None),  # no per-model key, env default 0 -> disabled
    ],
)
def test_injection_precedence_table(monkeypatch, per_model_value, env_default, expected_injected):
    """Precedence: [args].default_max_tokens > DEFAULT_MAX_TOKENS env (4096) >
    no injection when the resolved value is 0."""
    monkeypatch.setattr(main, "DEFAULT_MAX_TOKENS", env_default)
    args = {} if per_model_value is None else {"default_max_tokens": per_model_value}
    entry = {"alias": "test-model", "engine": "llama-cuda", "args": args}
    body = {"model": "test-model", "messages": []}

    main._inject_default_max_tokens(body, "test-model", entry)

    if expected_injected is None:
        assert "max_tokens" not in body
    else:
        assert body["max_tokens"] == expected_injected


@pytest.mark.parametrize(
    "body_overrides,expected_max_tokens",
    [
        ({"max_tokens": 256}, 256),  # explicit non-null value preserved untouched
        ({"max_tokens": None}, 4096),  # explicit null treated as omitted -> injected
        ({"max_tokens": 0}, 0),  # explicit 0 is a set, non-null value -> NOT injected over
        ({"n_predict": 8}, "OMIT"),  # n_predict counts as already-set -> no max_tokens injected
    ],
)
def test_explicit_value_preserved_including_null_and_zero(monkeypatch, body_overrides, expected_max_tokens):
    """Explicit client values (including 0) must never be overwritten; an
    explicit null is treated as omitted and IS eligible for injection."""
    monkeypatch.setattr(main, "DEFAULT_MAX_TOKENS", 4096)
    entry = {"alias": "test-model", "engine": "llama-cuda", "args": {}}
    body = {"model": "test-model", "messages": [], **body_overrides}

    main._inject_default_max_tokens(body, "test-model", entry)

    if expected_max_tokens == "OMIT":
        assert "max_tokens" not in body
    else:
        assert body["max_tokens"] == expected_max_tokens


def test_streaming_request_receives_same_injection(monkeypatch):
    """A stream:true request with no generation-length key must reach
    stream_upstream() with the injected max_tokens already in the body —
    identical treatment to the non-streaming path."""
    monkeypatch.setattr(main, "DEFAULT_MAX_TOKENS", 4096)
    monkeypatch.setattr(main, "check_auth", lambda request: None)

    entry = {"alias": "test-model", "engine": "llama-cuda", "args": {}}
    monkeypatch.setattr(main, "scan_registry", lambda: {"test-model": entry})

    async def fake_acquire_engine(alias, e):
        return None

    monkeypatch.setattr(main, "acquire_engine", fake_acquire_engine)

    captured = {}

    def fake_stream_upstream(url, body, request):
        captured["body"] = body

        async def gen():
            yield b"data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(main, "stream_upstream", fake_stream_upstream)

    client = TestClient(main.app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "stream": True, "messages": []},
    )

    assert resp.status_code == 200
    assert captured["body"]["max_tokens"] == 4096


def test_injection_logs_info_line_with_alias_and_value(monkeypatch, caplog):
    """Whenever the default is injected, an INFO log line must name both the
    model alias and the resolved injected value, so a truncated-looking
    answer is traceable to the default rather than a model failure."""
    monkeypatch.setattr(main, "DEFAULT_MAX_TOKENS", 4096)
    entry = {"alias": "gemma-4-12b-it-heretic", "engine": "llama-cuda", "args": {}}
    body = {"model": "gemma-4-12b-it-heretic", "messages": []}

    with caplog.at_level(logging.INFO, logger="ai-wrapper"):
        main._inject_default_max_tokens(body, "gemma-4-12b-it-heretic", entry)

    assert any(
        "gemma-4-12b-it-heretic" in record.getMessage() and "4096" in record.getMessage()
        for record in caplog.records
    )
