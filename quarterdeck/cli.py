"""Command-line interface.

Subcommands:
  spawn    register a new agent (optionally scheduled with a prompt template)
  list     show agents and their lifecycle state
  chat     read recent channel history, or post a message
  run      start the scheduler loop (Ctrl-C stops); scheduled agents wake, work, post
  forget   retire an agent (and drop its schedules)
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .agent_registry import AgentRegistry, RegistryError
from .chat import DEFAULT_CHANNEL, Chat
from .events import EventBus
from .scheduler import Scheduler, SchedulerError


def _log(msg: str) -> None:
    print(f"[quarterdeck] {msg}", file=sys.stderr, flush=True)


def _components(args) -> tuple[AgentRegistry, Chat, EventBus]:
    registry = AgentRegistry(home=args.home)
    bus = EventBus()
    chat = Chat(home=args.home, bus=bus)
    return registry, chat, bus


def _scheduler(args) -> tuple[Scheduler, AgentRegistry, Chat]:
    registry, chat, bus = _components(args)
    return Scheduler(registry, chat, bus=bus, home=args.home), registry, chat


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="quarterdeck",
        description="Agent control plane: wake/sleep Pinnace agents, keep their "
                    "identity, and share an IRC-style chat with them.",
    )
    p.add_argument("--version", action="version", version=f"quarterdeck {__version__}")
    p.add_argument("--home", default=None,
                   help="state dir (default: ~/.quarterdeck or $QUARTERDECK_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="register a new agent")
    sp.add_argument("--name", required=True, help="agent identity (e.g. scout)")
    sp.add_argument("--system", required=True, help="system prompt defining the agent's role")
    sp.add_argument("--model", default=None, help='model ref "provider:model" (default: $PINNACE_MODEL)')
    sp.add_argument("--sandbox", choices=("docker", "local"), default="docker",
                    help="sandbox kind (default: docker)")
    sp.add_argument("--max-turns", type=int, default=30, help="max turns per run (default: 30)")
    sp.add_argument("--interval", type=float, default=0,
                    help="schedule the agent to wake every N seconds (0 = unscheduled)")
    sp.add_argument("--prompt", default=None,
                    help="prompt template for scheduled wakes (required with --interval)")

    sp = sub.add_parser("list", help="show agents and their state")

    sp = sub.add_parser("chat", help="read or post in a channel")
    sp.add_argument("--channel", default=DEFAULT_CHANNEL, help="channel (default: #general)")
    sp.add_argument("--say", default=None, help="post TEXT to the channel as 'you'")
    sp.add_argument("-n", type=int, default=20, help="messages of history to print (default: 20)")

    sp = sub.add_parser("run", help="start the scheduler loop; Ctrl-C stops")

    sp = sub.add_parser("forget", help="retire an agent")
    sp.add_argument("--name", required=True, help="agent to retire")

    return p


def _resolve_home(args) -> None:
    args.home = args.home or None  # None -> components fall back to home_dir()


def cmd_spawn(args) -> int:
    _resolve_home(args)
    if args.interval and not args.prompt:
        _log("--interval needs --prompt: the agent has to know what to do when it wakes")
        return 2
    registry, chat, _ = _components(args)
    try:
        agent = registry.spawn(
            args.name, args.system, model=args.model,
            sandbox=args.sandbox, max_turns=args.max_turns)
    except RegistryError as e:
        _log(f"error: {e}")
        return 1
    _log(f"spawned agent {agent.name!r} (model={agent.model or '$PINNACE_MODEL'}, "
         f"sandbox={agent.sandbox}, max_turns={agent.max_turns})")
    if args.interval:
        scheduler = Scheduler(registry, chat, home=args.home)
        try:
            scheduler.add(f"{args.name}-wake", args.name, args.interval, args.prompt)
        except SchedulerError as e:
            _log(f"error: {e}")
            return 1
        _log(f"scheduled {args.name!r} to wake every {args.interval:g}s")
    return 0


def cmd_list(args) -> int:
    _resolve_home(args)
    registry, chat, _ = _components(args)
    agents = registry.list()
    scheduler = Scheduler(registry, chat, home=args.home)
    schedules = {e.agent_name: e for e in scheduler.list()}
    if not agents:
        _log("no agents. spawn one: quarterdeck spawn --name scout --system '...'")
        return 0
    for a in agents:
        sched = schedules.get(a.name)
        wake = f"wakes every {sched.interval:g}s" if sched else "unscheduled"
        print(f"{a.name:20} {a.state:8} {wake:28} model={a.model or '$PINNACE_MODEL'}")
    return 0


def cmd_chat(args) -> int:
    _resolve_home(args)
    _, chat, _ = _components(args)
    if args.say is not None:
        msg = chat.post(args.channel, "you", args.say)
        _log(f"posted to {msg['channel']} as you")
        return 0
    for msg in chat.history(args.channel, args.n):
        ts = time.strftime("%H:%M:%S", time.localtime(msg["ts"]))
        print(f"[{ts}] <{msg['author']}> {msg['text']}")
    return 0


def cmd_run(args) -> int:
    _resolve_home(args)
    sched, registry, _ = _scheduler(args)
    entries = sched.list()
    if not entries:
        _log("nothing scheduled. spawn with --interval N --prompt '...' to wake an agent")
        return 0
    _log(f"quarterdeck up: {len(entries)} schedule(s), "
         f"{len(registry.list())} agent(s); ctrl-c to sleep them")
    sched.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
    _log("all agents asleep")
    return 0


def cmd_forget(args) -> int:
    _resolve_home(args)
    registry, chat, _ = _components(args)
    try:
        registry.retire(args.name)
    except RegistryError as e:
        _log(f"error: {e}")
        return 1
    scheduler = Scheduler(registry, chat, home=args.home)
    dropped = scheduler.remove_for_agent(args.name)
    _log(f"retired {args.name!r}" + (f"; dropped {dropped} schedule(s)" if dropped else ""))
    return 0


_DISPATCH = {
    "spawn": cmd_spawn, "list": cmd_list, "chat": cmd_chat,
    "run": cmd_run, "forget": cmd_forget,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
