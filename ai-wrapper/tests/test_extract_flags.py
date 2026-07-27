import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

# Add nuc-infra/scripts to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "nuc-infra" / "scripts"))
from extract_flags import extract_flags, main

SAMPLE_HELP_OUTPUT = """
usage: llama-server [options]

options:
  -h, --help            show this help message and exit
  -c, --ctx-size N      size of the prompt context (default: 4096)
  -ngl, --n-gpu-layers N  number of layers to offload to GPU
  --flash-attn [on|off|auto]  enable flash attention
  -m FNAME, --model FNAME  model path
  --host HOST           ip address to listen (default: 127.0.0.1)
  --port PORT           port to listen (default: 8080)
  --draft-max N         number of tokens to draft
  --draft-min N         minimum tokens to draft
  --model-draft FNAME   draft model path
  -t N, --threads N     number of threads to use
  --temp N              temperature (default: 0.8)
"""


def test_extract_flags_valid_sample():
    flags = extract_flags(SAMPLE_HELP_OUTPUT)
    assert len(flags) >= 10
    expected_subset = {"-c", "--ctx-size", "-ngl", "--n-gpu-layers", "--flash-attn", "-m", "--model", "--draft-max"}
    assert expected_subset.issubset(set(flags))


def test_extract_flags_low_count():
    short_help = "usage: llama-server\n  -h, --help show help\n  -c N context"
    with pytest.raises(ValueError, match=r"Extraction failed: found only \d+ flags"):
        extract_flags(short_help)


def test_extract_flags_empty():
    with pytest.raises(ValueError, match=r"Extraction failed: found only 0 flags"):
        extract_flags("")


def test_cli_execution(tmp_path):
    output_file = tmp_path / "flags.toml"
    script_path = Path(__file__).resolve().parents[2] / "nuc-infra" / "scripts" / "extract_flags.py"

    # Test valid input via stdin
    proc = subprocess.run(
        [sys.executable, str(script_path), str(output_file)],
        input=SAMPLE_HELP_OUTPUT,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0
    assert output_file.exists()

    with open(output_file, "rb") as f:
        data = tomllib.load(f)
    assert "flags" in data
    assert "flags" in data["flags"]
    assert "--ctx-size" in data["flags"]["flags"]

    # Test invalid input via stdin -> non-zero exit code
    proc_bad = subprocess.run(
        [sys.executable, str(script_path), str(output_file)],
        input="invalid help text",
        text=True,
        capture_output=True,
    )
    assert proc_bad.returncode != 0
