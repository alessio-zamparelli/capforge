"""CLI: `capforge serve` for the MCP server, `capforge doctor` to check a deployment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _doctor() -> int:
    from .core import caps_dir, list_capabilities, state_dir, keep_tags

    problems: list[str] = []
    root, state = caps_dir(), state_dir()

    print(f"caps dir   {root}")
    print(f"state dir  {state}")
    print(f"keep tags  {keep_tags()}")
    print(f"python     {sys.version.split()[0]}")

    if not root.is_dir():
        problems.append(f"caps dir does not exist: {root} (create it, or set CAPFORGE_CAPS_DIR)")
    else:
        if not os.access(root, os.W_OK):
            problems.append(f"caps dir is not writable by uid {os.getuid()}: {root}")
        # Open WebUI only picks up skills under these paths, relative to the terminal home.
        home = Path.home()
        known = [home / ".agents" / "skills", home / ".cptr" / "skills", home / ".claude" / "skills", home / ".codex" / "skills"]
        if root not in [p.resolve() for p in known]:
            problems.append(
                f"caps dir is not one of the paths Open WebUI scans for terminal skills ({', '.join(str(p) for p in known)}); "
                "the model will not see capabilities in its per-turn skill manifest"
            )

    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"state dir is not writable: {exc}")

    caps = list_capabilities()
    print(f"caps found {len(caps)}")
    for cap in caps:
        mark = "ok  " if cap.draft_ready else "FAIL"
        print(f"  {mark} {cap.name:24} {cap.error or cap.description[:60]}")

    try:
        import fastmcp  # noqa: F401

        print(f"fastmcp    {fastmcp.__version__}")
    except ImportError:
        problems.append("fastmcp is not installed: pip install 'capforge[mcp]'")

    if problems:
        print("\nproblems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nno problems found")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from .server import mcp

    kwargs = {}
    if args.host:
        kwargs["host"] = args.host
    if args.port:
        kwargs["port"] = args.port
    if args.path:
        kwargs["path"] = args.path
    mcp.run(transport=args.transport, **kwargs)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capforge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument(
        "--transport",
        default=os.environ.get("CAPFORGE_TRANSPORT", "http"),
        choices=["http", "stdio", "sse", "streamable-http"],
    )
    serve.add_argument("--host", default=os.environ.get("CAPFORGE_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("CAPFORGE_PORT", "8099")))
    serve.add_argument("--path", default=os.environ.get("CAPFORGE_PATH", "/mcp"))
    serve.set_defaults(func=_serve)

    doctor = sub.add_parser("doctor", help="Check the deployment and print the caps root")
    doctor.set_defaults(func=lambda _a: _doctor())

    tools = sub.add_parser("tools", help="Print the MCP tool schemas as JSON")
    tools.set_defaults(func=lambda _a: _print_tools())

    args = parser.parse_args(argv)
    return args.func(args)


def _print_tools() -> int:
    import asyncio

    from .server import mcp

    async def dump():
        listed = await mcp.list_tools()
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in listed
        ]

    print(json.dumps(asyncio.run(dump()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
