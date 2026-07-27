#!/usr/bin/env python3
"""Extract command-line flags from llama-server --help output into a TOML schema file.

Usage:
    llama-server --help | python3 extract_flags.py /path/to/flags.toml
    python3 extract_flags.py <input_help_file> /path/to/flags.toml
"""

import json
import os
import re
import sys

FLAG_RE = re.compile(
    r'(?<!\S)(--(?:[a-zA-Z0-9][a-zA-Z0-9._-]*)|-[a-zA-Z][a-zA-Z0-9._-]*)(?=[=\s,:]|$)'
)
MIN_FLAG_COUNT = 10


def extract_flags(help_text: str) -> list[str]:
    """Parse flag names from --help output text. Returns sorted list of unique flags."""
    matches = FLAG_RE.findall(help_text)
    unique_flags = sorted(set(matches))
    if len(unique_flags) < MIN_FLAG_COUNT:
        raise ValueError(
            f"Extraction failed: found only {len(unique_flags)} flags (minimum required: {MIN_FLAG_COUNT})"
        )
    return unique_flags


def main() -> None:
    if len(sys.argv) == 2:
        help_text = sys.stdin.read()
        output_path = sys.argv[1]
    elif len(sys.argv) == 3:
        input_path = sys.argv[1]
        output_path = sys.argv[2]
        with open(input_path, "r", encoding="utf-8") as f:
            help_text = f.read()
    else:
        sys.stderr.write("Usage: extract_flags.py [input_file] <output_file>\n")
        sys.exit(1)

    try:
        flags = extract_flags(help_text)
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    toml_content = f"[flags]\nflags = {json.dumps(flags)}\n"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(toml_content)


if __name__ == "__main__":
    main()
