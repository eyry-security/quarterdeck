"""Quarterdeck service entrypoint: the long-running local control plane.

Run directly with ``python -m quarterdeck.server`` or through
``quarterdeck serve``. The service owns the agent daemon, scheduler, event
rules, ChatOps router, and the web/room API behind one Runtime.

Configuration (explicit flags win over environment):
- ``QD_PORT``       bind port, default 4097
- ``QD_STATE_DIR``  persistent state directory, default /var/lib/quarterdeck
- ``QUARTERDECK_HOME`` is accepted as a legacy state-dir fallback
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .runtime import Runtime

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4097
DEFAULT_STATE_DIR = Path("/var/lib/quarterdeck")


def _log(event: str, **fields) -> None:
    """Emit one structured JSON log line per lifecycle event."""
    record = {"event": event, **fields}
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


def resolve_port(port: int | None = None) -> int:
    if port is not None:
        return int(port)
    raw = os.environ.get("QD_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"QD_PORT must be an integer, got {raw!r}") from exc


def resolve_state_dir(state_dir: str | Path | None = None) -> Path:
    if state_dir is not None and str(state_dir).strip():
        return Path(state_dir)
    for name in ("QD_STATE_DIR", "QUARTERDECK_HOME"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return Path(raw)
    return DEFAULT_STATE_DIR


def resolve_host(host: str | None = None) -> str:
    return (host or DEFAULT_HOST).strip() or DEFAULT_HOST


def _make_server(uvicorn, config):
    """Build a uvicorn server whose SIGTERM path returns instead of re-raising.

    Uvicorn normally restores the default handler and re-raises the captured
    signal after draining, which terminates the process before the Runtime
    can shut down. Here the process-level handler only flips ``should_exit``;
    ``run`` then drains connections and returns normally, and the caller stops
    the daemon/scheduler/rules in a ``finally`` block.
    """
    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            # Installed by _serve below; never restore-and-re-raise.
            yield

    return Server(config)


def _serve(app, host: str, port: int) -> None:
    import uvicorn

    # Caddy terminates TLS on this host and forwards X-Forwarded-* over
    # loopback. Trust those headers so request.url.scheme is https and secure
    # cookies / absolute URLs reflect the public host.
    forwarded = os.environ.get("QD_FORWARDED_ALLOW_IPS", "127.0.0.1").strip()
    server = _make_server(uvicorn, uvicorn.Config(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded or "127.0.0.1",
    ))
    previous: dict[int, object] = {}

    if threading.current_thread() is threading.main_thread():
        def _request_shutdown(signum, frame):
            server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, _request_shutdown)
            except (ValueError, OSError):
                pass
    try:
        server.run()
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def run(
    *,
    host: str | None = None,
    port: int | None = None,
    state_dir: str | Path | None = None,
    allow_local: bool = False,
) -> int:
    """Start the service and block until SIGTERM/SIGINT (or a server error).

    The HTTP server drains connections before returning; the ``finally`` block
    then shuts the runtime down in reverse boot order.
    """
    from .web import create_app

    host = resolve_host(host)
    port = resolve_port(port)
    home = resolve_state_dir(state_dir)
    home.mkdir(parents=True, exist_ok=True)

    runtime = Runtime(home, allow_local=allow_local)
    before = {agent.name for agent in runtime.registry.list()}
    _log("starting", host=host, port=port, state_dir=str(home))

    runtime.start()
    agents = sorted(agent.name for agent in runtime.registry.list())
    seeded = [name for name in agents if name not in before]
    _log(
        "ready",
        host=host,
        port=port,
        state_dir=str(home),
        agents=agents,
        seeded_agents=seeded,
    )
    try:
        _serve(create_app(runtime=runtime), host, port)
    finally:
        runtime.stop()
        _log("stopped", host=host, port=port, state_dir=str(home))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m quarterdeck.server",
        description="Run Quarterdeck as a persistent local service.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None,
                        help=f"bind port (default: $QD_PORT or {DEFAULT_PORT})")
    parser.add_argument("--state-dir", default=None,
                        help="state dir (default: $QD_STATE_DIR or "
                             f"{DEFAULT_STATE_DIR})")
    parser.add_argument("--allow-local", action="store_true",
                        help="allow local-sandbox agents (development only)")
    parser.add_argument("--version", action="version",
                        version=f"quarterdeck {__version__}")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(
            host=args.host,
            port=args.port,
            state_dir=args.state_dir,
            allow_local=args.allow_local,
        )
    except ValueError as exc:
        _log("config-error", error=str(exc))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
