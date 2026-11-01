"""The contract the model reads before writing a capability, and the scaffolds it starts from."""

from __future__ import annotations

CONTRACT = """\
# Capability contract

A capability is one folder under the caps root. The folder name is the capability name and is
shared between this MCP server and the terminal: what you write there is what this server runs.

    <caps>/<name>/
        SKILL.md          frontmatter + instructions (read by Open WebUI every turn)
        main.py           entrypoint (name configurable via `entrypoint:` in frontmatter)
        tests/test_main.py optional; pytest runs if any test_*.py exists
        <anything>        helper modules, data files, templates

## Process contract

The entrypoint is executed as a fresh subprocess, once per call:

    <venv>/bin/python main.py < /dev/stdin

* stdin  : one JSON object with the call arguments. Never argv, never environment variables.
* stdout : one JSON value. It may be the whole output or the last non-empty line; anything
           before it is kept in the log the caller sees, so print diagnostics freely.
* stderr : logs. Shown to the caller on failure.
* exit 0 : success. Any other exit code is a failure and stderr is reported.

Return `{"error": "..."}` to signal a handled failure with a message meant for the model.
Raise only for bugs; an uncaught exception is a failed call.

## Rules that matter

1. **Validate your input.** Arguments come from a language model. Reject anything you did not
   expect with a specific message: "start_date must be YYYY-MM-DD, got 'next tuesday'".
2. **No network by default.** Assume none. If you need it, say so in SKILL.md so a human can
   decide; do not silently depend on it.
3. **Write only under CAPFORGE_DATA_DIR.** It is per-capability and per-version. Everything
   else in the filesystem is read-only in spirit: the hub, other capabilities and the versions
   tree are not yours to modify.
4. **No secrets.** Environment is filtered down to PATH, locale and TLS settings. pip index
   settings are visible to the installer only, never to your process. There is no place to
   put an API key in this design, by construction.
5. **Be fast.** Calls time out (default 120s, CAPFORGE_RUN_TIMEOUT). Long work belongs in a
   subprocess you poll, or in a file you write and read back later.
6. **Deterministic output.** Same args plus same data directory must give the same result.
   Sort collections, pin formats, never emit timestamps you did not ask for.

## Dependencies

List them in SKILL.md frontmatter and the venv is built for you, per capability, keyed by the
requirement set:

    ---
    name: my_cap
    description: One sentence, shown to the model in the per-turn manifest.
    packages: [pypdf>=5, httpx]
    ---

Package entries are plain requirement specs: no spaces, commas, quotes, URLs or leading
`-`. That keeps them safe as pip arguments and unambiguous in the frontmatter. At most 50
per capability. Call install_packages(name, packages) to add them now and record them;
promote_capability(name, packages=[...]) replaces the set (including to remove one).
Installs are isolated from the hub and from every other capability. stdlib-only is the
happy path.

## Versioning

The working copy is the draft. `test_capability` runs the draft. `promote_capability` snapshots
the draft into an immutable tag, records it in a git commit, and makes it the version
`run_capability` executes. The tag carries its own SKILL.md, so later draft edits cannot
change its entrypoint or dependencies. `rollback_capability` points back at an older tag
without touching the draft. The last N tags are kept (default 5, CAPFORGE_KEEP_TAGS); older
ones are deleted together with their per-version data and unused virtualenvs.
"""

SCAFFOLD_MAIN = '''\
"""Entry point for capability {name}.

Reads one JSON object from stdin, writes one JSON value to stdout.
Edit freely: nothing here is required except the stdin/stdout contract.
"""

from __future__ import annotations

import json
import os
import sys


def handle(args: dict) -> dict:
    # Replace with the real work. Keep the validation: arguments come from a model.
    text = args.get("text")
    if not isinstance(text, str):
        return {{"error": "text is required and must be a string"}}
    return {{"text": text, "length": len(text)}}


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        args = json.loads(raw) if raw else {{}}
    except json.JSONDecodeError as exc:
        print(f"stdin is not valid JSON: {{exc}}", file=sys.stderr)
        return 2
    if not isinstance(args, dict):
        print("stdin must be a JSON object", file=sys.stderr)
        return 2
    try:
        result = handle(args)
    except Exception as exc:  # noqa: BLE001 - a bug must not look like a crash
        print(f"unhandled error: {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

SCAFFOLD_SKILL = """\
---
name: {name}
description: {description}
entrypoint: main.py
packages: [{packages}]
---

# {name}

What this does, in two or three sentences. Be concrete: the model reads this to decide
whether the capability is the right one for the job.

## Arguments

| name | type | required | meaning |
|------|------|----------|---------|
| text | string | yes | the value to measure |

## Returns

    {{"text": "hello", "length": 5}}

## Notes

Anything the caller must know: limits, formats, what it refuses, what it never touches.
"""

SCAFFOLD_TEST = '''\
"""Tests for {name}. These run before a promotion, and a failure blocks it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_happy_path() -> None:
    assert run({{"text": "hello"}})["length"] == 5


def test_rejects_bad_input() -> None:
    assert "error" in run({{"text": 42}})
'''

GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.venv/\n"
