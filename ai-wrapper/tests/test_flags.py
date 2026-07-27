import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Add ai-wrapper directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


@pytest.fixture(autouse=True)
def clear_schema_cache():
    main._schema_cache.clear()
    main.active_alias = None


@pytest.mark.asyncio
async def test_vram_isolation():
    """Verify get_engine_schema runs podman cat WITHOUT --device nvidia.com/gpu=all."""
    podman_calls = []

    async def mock_podman(*args, **kwargs):
        podman_calls.append(args)
        if args[0] == "image" and args[1] == "inspect":
            return "sha256:1111222233334444555566667777888899990000111122223333444455556666"
        if args[0] == "run":
            return '[flags]\nflags = ["--ctx-size", "-c", "--flash-attn"]\n'
        return ""

    with patch.object(main, "podman", side_effect=mock_podman):
        schema = await main.get_engine_schema("llama-atomic")
        assert schema == {"--ctx-size", "-c", "--flash-attn"}

    # Inspect all podman calls and ensure --device nvidia.com/gpu=all was NEVER passed
    for call_args in podman_calls:
        assert "--device" not in call_args
        assert "nvidia.com/gpu=all" not in call_args


@pytest.mark.asyncio
async def test_digest_keyed_cache():
    """Verify get_engine_schema caches schema by image digest sha256:..."""
    podman_calls = []

    async def mock_podman(*args, **kwargs):
        podman_calls.append(args)
        if args[0] == "image" and args[1] == "inspect":
            return "sha256:digest123456789"
        if args[0] == "run":
            return '[flags]\nflags = ["--ctx-size", "--temp"]\n'
        return ""

    with patch.object(main, "podman", side_effect=mock_podman):
        schema1 = await main.get_engine_schema("llama-cuda")
        assert schema1 == {"--ctx-size", "--temp"}
        assert "sha256:digest123456789" in main._schema_cache

        # Call again - should hit cache and not invoke podman run
        schema2 = await main.get_engine_schema("llama-cuda")
        assert schema2 == {"--ctx-size", "--temp"}

    # podman run should have been called only once
    run_calls = [c for c in podman_calls if c[0] == "run"]
    assert len(run_calls) == 1


def test_security_validation():
    """Verify strict regex validation on extra flags and path safety on draft_model."""
    # Command injection attempt in extra
    bad_args_1 = {"extra": ["; rm -rf /"]}
    assert not main._validate_args(bad_args_1, "test_config")

    bad_args_2 = {"extra": ["--flag$(whoami)"]}
    assert not main._validate_args(bad_args_2, "test_config")

    # Path traversal attempt in draft_model
    bad_args_3 = {"draft_model": "../../../etc/passwd"}
    assert not main._validate_args(bad_args_3, "test_config")

    # Safe args
    good_args = {"ctx_size": 4096, "extra": ["--temp", "0.7"]}
    assert main._validate_args(good_args, "test_config")


@pytest.mark.asyncio
async def test_three_layer_validation_and_503():
    """Verify unknown extra flag causes _validate_args failure and acquire_engine fast 503 rejection."""
    schema = {"--ctx-size", "-c", "--n-gpu-layers", "-ngl", "--temp"}

    # Valid extra flag in schema
    valid_args = {"extra": ["--temp", "0.7"]}
    assert main._validate_args(valid_args, "test_config", schema_flags=schema)

    # Invalid extra flag not in schema
    invalid_args = {"extra": ["--invalid-flag", "123"]}
    assert not main._validate_args(invalid_args, "test_config", schema_flags=schema)

    # Fast 503 rejection in acquire_engine before engine swap
    entry = {
        "alias": "test-model",
        "engine": "llama-cuda",
        "folder": "test-folder",
        "file": "model.gguf",
        "args": {"extra": ["--unsupported-flag"]},
    }

    with patch.object(main, "get_engine_schema", return_value=schema):
        with patch.object(main, "stop_engine", new_callable=AsyncMock) as mock_stop:
            with pytest.raises(HTTPException) as exc_info:
                await main.acquire_engine("test-model", entry)

            assert exc_info.value.status_code == 503
            assert "--unsupported-flag" in str(exc_info.value.detail)
            # stop_engine must NOT have been called (fast fail before swap)
            mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_fallback():
    """Verify engine without flags.toml falls back to base allowlist with warning."""
    async def mock_podman(*args, **kwargs):
        if args[0] == "image" and args[1] == "inspect":
            return "sha256:digest_no_schema"
        if args[0] == "run":
            # Simulate missing file / non-zero return
            return ""
        return ""

    with patch.object(main, "podman", side_effect=mock_podman):
        schema = await main.get_engine_schema("llama-tom")
        assert schema is None

    # When schema is None, extra flags are validated using base allowlist + regex
    args = {"extra": ["--temp", "0.7"]}
    assert main._validate_args(args, "test_config", schema_flags=None)
