"""CLI: `capforge serve` for the MCP server, `capforge doctor` to check a deployment."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from contextlib import contextmanager
from typing import Any, Literal, cast

Transport = Literal["http", "stdio", "sse", "streamable-http"]
TRANSPORTS = {"http", "stdio", "sse", "streamable-http"}

log = logging.getLogger("capforge")

# Terminal skills live in one of these directories under the terminal user's home.
# The terminal usually runs in another container, so its home is not Path.home() here.
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
            print(f"web        {web.web_host()}:{web_port} ui+api+git+cap console={console}")
            for name, record in sorted(web.read_registry().items()):
                print(f"  served {name:24} tag={record.get('tag')} desired={record.get('desired')}")
    except Exception as exc:  # a broken web install must not crash doctor
        problems.append(f"web serving is not usable: {exc}")

    from .core import mcp_token

    transport = os.environ.get("CAPFORGE_TRANSPORT", "http").strip().lower() or "http"
    if mcp_token():
        print("mcp auth   bearer token configured")
    else:
        print("mcp auth   MISSING (CAPFORGE_MCP_TOKEN)")
        if transport != "stdio":
            problems.append("CAPFORGE_MCP_TOKEN is required for HTTP MCP transport")

    if not root.is_dir():
        problems.append(f"caps dir does not exist: {root} (create it, or set CAPFORGE_CAPS_DIR)")
    else:
        if not os.access(root, os.W_OK):
            problems.append(f"caps dir is not writable by uid {os.getuid()}: {root}")

    try:
        import shutil

        uv = shutil.which("uv")
        print(f"uv         {uv or 'MISSING'}")
        if not uv:
            problems.append("uv is missing; capabilities cannot build their environments")
        git = shutil.which("git")
        print(f"git        {git or 'MISSING'}")
        if not git:
            problems.append("git is missing; the capability repository cannot run")
        from . import sandbox

        abi = sandbox.abi_version()
        print(f"landlock   ABI {abi if abi is not None else 'unavailable'}")
        if abi is None:
            problems.append("Landlock is unavailable; CAPFORGE_SANDBOX=require will refuse to run")
    except Exception as exc:
        problems.append(f"sandbox/uv/git check failed: {exc}")

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
    from .core import set_subreaper

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
    from . import core, web

    if core.mcp_token() is None:
        print(
            "CAPFORGE_MCP_TOKEN is required for HTTP MCP transport; "
            "set it to a long random string (for example: openssl rand -hex 32).",
            file=sys.stderr,
        )
        return 2
    # Detached grandchildren of capability calls re-parent here, so the stray
    # sweep can find and kill them (nothing survives a call).
    set_subreaper()
    if web.web_port() != 0:
        web.restore()
    from . import staging

    staging.sweep_backups()
    server_module.RUNTIME_TRANSPORT = "http"
    _run_listeners(server_module.mcp, web, host, port, path)
    return 0


def _run_listeners(mcp: Any, web: Any, host: str, port: int, mcp_path: str) -> None:
    """FastMCP on :8099/mcp and the web app on :8080, one process, one signal set.

    Both uvicorn servers have their own signal capture disabled; ours sets
    should_exit on both and kills served children, so nothing is orphaned."""
    import anyio
    import uvicorn

    class _QuietServer(uvicorn.Server):
        # Signal handling is owned by this function, not by either server.
        @contextmanager
        def capture_signals(self):
            yield

    servers = [
        _QuietServer(
            uvicorn.Config(
                mcp.http_app(path=mcp_path, transport="http"),
                host=host,
                port=port,
                log_level=_uvicorn_log_level(),
            )
        )
    ]
    web_port = web.web_port()
    if web_port == 0:
        log.info("web listener disabled (CAPFORGE_WEB_PORT=0)")
    else:
        servers.append(
            _QuietServer(
                uvicorn.Config(
                    web.asgi_app(),
                    host=web.web_host(),
                    port=web_port,
                    log_level=_uvicorn_log_level(),
                )
            )
        )

    async def _handle_signals() -> None:
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for _sig in signals:
                for server in servers:
                    server.should_exit = True
                web.shutdown()
                break

    async def _main() -> None:
        async with anyio.create_task_group() as group:
            for server in servers:
                group.start_soon(server.serve)
            group.start_soon(_handle_signals)

    anyio.run(_main)


def _uvicorn_log_level() -> str:
    return os.environ.get("CAPFORGE_LOG_LEVEL", "INFO").lower()


def _promote(args: argparse.Namespace) -> int:
    from .server import promote_capability

    result = promote_capability(
        args.name, tag=args.tag, note=args.note, force=args.force
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def _test(args: argparse.Namespace) -> int:
    from .server import test_capability

    result = test_capability(args.name)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def _delete(args: argparse.Namespace) -> int:
    from .server import delete_capability

    if not args.yes:
        answer = input(f"Delete capability {args.name!r} and all state? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted", file=sys.stderr)
            return 1
    result = delete_capability(args.name, args.name)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


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

    promote = sub.add_parser("promote", help="Operator recovery: test and promote the server draft")
    promote.add_argument("name")
    promote.add_argument("--tag", default=None)
    promote.add_argument("--note", default="")
    promote.add_argument("--force", action="store_true")
    promote.set_defaults(func=_promote)

    test = sub.add_parser("test", help="Operator recovery: test the server draft")
    test.add_argument("name")
    test.set_defaults(func=_test)

    delete = sub.add_parser("delete", help="Delete a capability and all of its server state")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true", dest="yes")
    delete.set_defaults(func=_delete)

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
