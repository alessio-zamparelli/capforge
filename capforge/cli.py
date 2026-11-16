"""CLI: `capforge serve` for the MCP server, `capforge doctor` to check a deployment."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

Transport = Literal["http", "stdio", "sse", "streamable-http"]
TRANSPORTS = {"http", "stdio", "sse", "streamable-http"}

log = logging.getLogger("capforge")

# Terminal skills live in one of these directories under the terminal user's home.
# The terminal usually runs in another container, so its home is not Path.home() here.
SKILL_DIRS = {
    (".agents", "skills"),
    (".cptr", "skills"),
    (".claude", "skills"),
    (".codex", "skills"),
}


def _is_skills_path(path: Path) -> bool:
    """Accept any <home>/.agents/skills-style path, regardless of whose home it is."""
    parts = path.resolve().parts
    return len(parts) >= 2 and (parts[-2], parts[-1]) in SKILL_DIRS


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _doctor() -> int:
    from .core import caps_dir, keep_tags, list_capabilities, state_dir

    problems: list[str] = []
    root, state = caps_dir(), state_dir()

    print(f"caps dir   {root}")
    print(f"state dir  {state}")
    print(f"keep tags  {keep_tags()}")
    print(f"python     {sys.version.split()[0]}")
    print(f"log level  {os.environ.get('CAPFORGE_LOG_LEVEL', 'INFO')}")

    try:
        from . import web

        web_port = web.web_port()
        if web_port == 0:
            print("web listener disabled (CAPFORGE_WEB_PORT=0)")
        else:
            token = web.web_token()
            console = "token-gated" if token else "disabled (no CAPFORGE_WEB_TOKEN)"
            print(f"web        {web.web_host()}:{web_port} console={console}")
            for name, record in sorted(web.read_registry().items()):
                print(f"  served {name:24} tag={record.get('tag')} desired={record.get('desired')}")
    except Exception as exc:  # a broken web install must not crash doctor
        problems.append(f"web serving is not usable: {exc}")

    if not root.is_dir():
        problems.append(f"caps dir does not exist: {root} (create it, or set CAPFORGE_CAPS_DIR)")
    else:
        if not os.access(root, os.W_OK):
            problems.append(f"caps dir is not writable by uid {os.getuid()}: {root}")
        # Open WebUI scans these names under the *terminal* user's home. The terminal
        # normally runs in another container, so do not compare against Path.home().
        if not _is_skills_path(root):
            problems.append(
                f"caps dir does not look like a terminal skills path: {root}. "
                "Mount the same host folder at <terminal-home>/.agents/skills "
                "(or .cptr/.claude/.codex) and point CAPFORGE_CAPS_DIR at capforge's mount; "
                "the terminal's home can differ from this container's home."
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
        import fastmcp

        print(f"fastmcp    {getattr(fastmcp, '__version__', 'unknown')}")
    except Exception as exc:  # a broken install must not crash doctor
        problems.append(f"fastmcp is not usable: {exc}")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nno problems found")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import signal  # noqa: F401 - referenced by _handle_signals below

    from . import server as server_module
    from .core import ensure_workspace_skill, set_subreaper

    ensure_workspace_skill()
    transport = args.transport or "http"
    if transport not in TRANSPORTS:
        print(f"invalid transport {transport!r}; choose one of {sorted(TRANSPORTS)}", file=sys.stderr)
        return 2
    host = args.host or os.environ.get("CAPFORGE_HOST", "0.0.0.0")
    port = args.port if args.port is not None else _env_int("CAPFORGE_PORT", 8099)
    path = args.path or os.environ.get("CAPFORGE_PATH", "/mcp")
    if transport == "stdio":
        # No HTTP surface: web serving and the console make no sense here.
        server_module.RUNTIME_TRANSPORT = "stdio"
        server_module.mcp.run(transport=cast(Transport, "stdio"))
        return 0
    from . import web

    # Detached grandchildren of capability calls re-parent here, so the stray
    # sweep can find and kill them (nothing survives a call).
    set_subreaper()
    web.restore()
    server_module.RUNTIME_TRANSPORT = "http"
    _run_two_listeners(server_module.mcp, web, host, port, path)
    return 0


def _run_two_listeners(mcp: Any, web: Any, host: str, port: int, mcp_path: str) -> None:
    """FastMCP on :8099/mcp, the web app on :8080, one process, one signal set.

    Both uvicorn servers have their own signal capture disabled; ours sets
    should_exit on both and kills served children, so nothing is orphaned."""
    import anyio
    import uvicorn

    mcp_app = mcp.http_app(path=mcp_path, transport="http")
    web_port = web.web_port()
    web_app = web.asgi_app() if web_port != 0 else None
    if web_app is None:
        log.info("web listener disabled (CAPFORGE_WEB_PORT=0)")

    class _QuietServer(uvicorn.Server):
        # Signal handling is owned by this function, not by either server.
        @contextmanager
        def capture_signals(self):
            yield

    mcp_server = _QuietServer(
        uvicorn.Config(mcp_app, host=host, port=port, log_level=_uvicorn_log_level())
    )
    web_server = (
        _QuietServer(
            uvicorn.Config(
                web_app, host=web.web_host(), port=web_port, log_level=_uvicorn_log_level()
            )
        )
        if web_app is not None
        else None
    )

    async def _handle_signals() -> None:
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for _sig in signals:
                mcp_server.should_exit = True
                if web_server is not None:
                    web_server.should_exit = True
                web.shutdown()
                break

    async def _main() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(mcp_server.serve)
            if web_server is not None:
                group.start_soon(web_server.serve)
            group.start_soon(_handle_signals)

    anyio.run(_main)


def _uvicorn_log_level() -> str:
    return os.environ.get("CAPFORGE_LOG_LEVEL", "INFO").lower()


def main(argv: list[str] | None = None) -> int:
    level_name = os.environ.get("CAPFORGE_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(prog="capforge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server")
    serve.add_argument(
        "--transport",
        default=os.environ.get("CAPFORGE_TRANSPORT", "http"),
        choices=sorted(TRANSPORTS),
    )
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--path", default=None)
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
