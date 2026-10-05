"""Quarterdeck CLI: agents, schedules, durable events, rules, and local ChatOps."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .agent_registry import AgentRegistry, RegistryError, home_dir
from .chat import DEFAULT_CHANNEL, Chat
from .chatops import ChatOps
from .events import Event, EventBus, EventStore
from .integrations import parse_aplomado_event
from .rules import Rule, RuleEngine, RuleError, RuleStore
from .scheduler import Scheduler, SchedulerError


def _log(msg: str) -> None:
    print(f"[quarterdeck] {msg}", file=sys.stderr, flush=True)


def _resolve_home(args) -> Path:
    args.home = args.home or None
    return Path(args.home) if args.home else home_dir()


def _components(args) -> tuple[AgentRegistry, Chat, EventBus]:
    home = _resolve_home(args)
    registry = AgentRegistry(home=home)
    bus = EventBus(store=EventStore(home))
    chat = Chat(home=home, bus=bus)
    return registry, chat, bus


def _scheduler(args):
    registry, chat, bus = _components(args)
    scheduler = Scheduler(
        registry, chat, bus=bus, home=_resolve_home(args),
        allow_local=getattr(args, "allow_local", False),
    )
    return scheduler, registry, chat, bus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quarterdeck",
        description="Persistent Pinnace agent orchestration and local ChatOps.",
    )
    parser.add_argument("--version", action="version", version=f"quarterdeck {__version__}")
    parser.add_argument("--home", default=None,
                        help="state dir (default: ~/.quarterdeck or $QUARTERDECK_HOME)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    spawn = sub.add_parser("spawn", help="register a new agent")
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--system", required=True)
    spawn.add_argument("--model", default=None,
                       help="provider:model (default: anthropic:claude-opus-4-6 via Pinnace)")
    spawn.add_argument("--sandbox", choices=("docker", "local"), default="docker")
    spawn.add_argument("--max-turns", type=int, default=30)
    spawn.add_argument("--interval", type=float, default=0)
    spawn.add_argument("--prompt", default=None)

    sub.add_parser("list", help="show agents and lifecycle state")

    chat = sub.add_parser("chat", help="read or post; '@agent prompt' wakes an agent")
    chat.add_argument("--channel", default=DEFAULT_CHANNEL)
    chat.add_argument("--say", default=None)
    chat.add_argument("-n", type=int, default=20)
    chat.add_argument("--allow-local", action="store_true",
                      help="allow a mentioned local-sandbox agent (dev only)")

    run = sub.add_parser("run", help="run schedules and in-process event/chat routing")
    run.add_argument("--allow-local", action="store_true",
                     help="allow unattended local-sandbox agents (dev only)")

    wake = sub.add_parser("wake", help="run one named agent now")
    wake.add_argument("--agent", required=True)
    wake.add_argument("--prompt", required=True)
    wake.add_argument("--channel", default=DEFAULT_CHANNEL)
    wake.add_argument("--allow-local", action="store_true")

    forget = sub.add_parser("forget", help="retire an agent")
    forget.add_argument("--name", required=True)

    emit = sub.add_parser("event", help="persist and route one event")
    emit.add_argument("--type", required=True)
    emit.add_argument("--payload", default="{}", help="JSON object")
    emit.add_argument("--id")
    emit.add_argument("--source", default="cli")
    emit.add_argument("--allow-local", action="store_true")

    events = sub.add_parser("events", help="show persisted event history")
    events.add_argument("--type")
    events.add_argument("-n", type=int, default=20)
    events.add_argument("--json", action="store_true")

    add = sub.add_parser("rule-add", help="add an exact-type event rule")
    add.add_argument("--name", required=True)
    add.add_argument("--event", required=True)
    add.add_argument("--action", choices=("agent", "alert"), required=True)
    add.add_argument("--agent")
    add.add_argument("--prompt", default="Review this untrusted event:\n{event_json}")
    add.add_argument("--channel", default=DEFAULT_CHANNEL)
    sub.add_parser("rule-list", help="list event rules")
    remove = sub.add_parser("rule-remove", help="remove an event rule")
    remove.add_argument("--name", required=True)

    ingest = sub.add_parser("ingest-aplomado", help="persist and route Aplomado event JSONL")
    ingest.add_argument("--file", required=True, help="event JSONL path, or '-' for stdin")
    ingest.add_argument("--allow-local", action="store_true")

    serve = sub.add_parser("serve", help="run the webchat server (KiwiIRC-style agent room)")
    serve.add_argument("--port", type=int, default=8420)
    serve.add_argument("--host", default="127.0.0.1")
    return parser


def cmd_spawn(args) -> int:
    _resolve_home(args)
    if args.interval and not args.prompt:
        _log("--interval needs --prompt")
        return 2
    registry, chat, _bus = _components(args)
    try:
        agent = registry.spawn(
            args.name, args.system, model=args.model,
            sandbox=args.sandbox, max_turns=args.max_turns,
        )
        if args.interval:
            Scheduler(registry, chat, home=_resolve_home(args)).add(
                f"{args.name}-wake", args.name, args.interval, args.prompt
            )
    except (RegistryError, SchedulerError) as exc:
        _log(f"error: {exc}")
        return 1
    _log(f"spawned agent {agent.name!r} (model={agent.model or 'anthropic:claude-opus-4-6'})")
    return 0


def cmd_list(args) -> int:
    registry, chat, _bus = _components(args)
    schedules = {entry.agent_name: entry for entry in Scheduler(
        registry, chat, home=_resolve_home(args), runner=lambda agent, prompt: ""
    ).list()}
    if not registry.list():
        _log("no agents; use quarterdeck spawn")
        return 0
    for agent in registry.list():
        schedule = schedules.get(agent.name)
        wake = f"every {schedule.interval:g}s" if schedule else "unscheduled"
        print(f"{agent.name:20} {agent.state:8} {wake:20} model={agent.model or 'anthropic:claude-opus-4-6'}")
    return 0


def cmd_chat(args) -> int:
    scheduler, registry, chat, bus = _scheduler(args)
    if args.say is not None:
        router = ChatOps(registry, scheduler, chat, bus)
        message = chat.post(args.channel, "you", args.say)
        future = router.route(message)
        if future is not None:
            future.result()
        scheduler.stop()
        _log(f"posted to {message['channel']} as you")
        return 0
    for message in chat.history(args.channel, args.n):
        stamp = time.strftime("%H:%M:%S", time.localtime(message["ts"]))
        print(f"[{stamp}] <{message['author']}> {message['text']}")
    scheduler.stop()
    return 0


def cmd_run(args) -> int:
    scheduler, registry, chat, bus = _scheduler(args)
    rules = RuleEngine(RuleStore(_resolve_home(args)), registry, scheduler, chat, bus)
    chatops = ChatOps(registry, scheduler, chat, bus)
    rules.start()
    chatops.start()
    _log(f"quarterdeck up: {len(scheduler.list())} schedule(s), {len(registry.list())} agent(s)")
    scheduler.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        chatops.stop()
        rules.stop()
        scheduler.stop()
    _log("all agents asleep")
    return 0


def cmd_wake(args) -> int:
    scheduler, _registry, _chat, _bus = _scheduler(args)
    try:
        scheduler.wake(args.agent, args.prompt, args.channel).result()
    except SchedulerError as exc:
        _log(f"error: {exc}")
        scheduler.stop()
        return 1
    scheduler.stop()
    return 0


def cmd_forget(args) -> int:
    registry, chat, _bus = _components(args)
    try:
        registry.retire(args.name)
    except RegistryError as exc:
        _log(f"error: {exc}")
        return 1
    scheduler = Scheduler(registry, chat, home=_resolve_home(args), runner=lambda a, p: "")
    dropped = scheduler.remove_for_agent(args.name)
    scheduler.stop()
    _log(f"retired {args.name!r}" + (f"; dropped {dropped} schedule(s)" if dropped else ""))
    return 0


def _route_event(args, event: Event) -> bool:
    scheduler, registry, chat, bus = _scheduler(args)
    engine = RuleEngine(RuleStore(_resolve_home(args)), registry, scheduler, chat, bus)
    engine.start()
    created = bus.publish(event)
    engine.stop()
    scheduler.stop()
    return created


def cmd_event(args) -> int:
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        _log(f"invalid payload JSON: {exc}")
        return 2
    if not isinstance(payload, dict):
        _log("event payload must be a JSON object")
        return 2
    event = Event(
        type=args.type, payload=payload, id=args.id or Event(type=args.type).id,
        source=args.source,
    )
    if not _route_event(args, event):
        _log(f"duplicate event {event.id}")
        return 1
    print(event.id)
    return 0


def cmd_events(args) -> int:
    store = EventStore(_resolve_home(args))
    for event in store.history(event_type=args.type, limit=args.n):
        if args.json:
            print(json.dumps(event.to_dict(), separators=(",", ":")))
        else:
            print(f"{event.id} {event.type} source={event.source} ts={event.ts:.3f}")
    return 0


def cmd_rule_add(args) -> int:
    registry, _chat, _bus = _components(args)
    if args.action == "agent" and not registry.exists(args.agent or ""):
        _log(f"error: no agent named {args.agent!r}")
        return 1
    try:
        RuleStore(_resolve_home(args)).add(Rule(
            name=args.name, event_type=args.event, action=args.action,
            agent_name=args.agent, prompt=args.prompt, channel=args.channel,
        ))
    except RuleError as exc:
        _log(f"error: {exc}")
        return 1
    return 0


def cmd_rule_list(args) -> int:
    for rule in RuleStore(_resolve_home(args)).list():
        print(f"{rule.name:20} {rule.event_type:28} {rule.action:6} {rule.agent_name or '-'} -> {rule.channel}")
    return 0


def cmd_rule_remove(args) -> int:
    try:
        RuleStore(_resolve_home(args)).remove(args.name)
    except RuleError as exc:
        _log(f"error: {exc}")
        return 1
    return 0


def cmd_ingest_aplomado(args) -> int:
    if args.file == "-":
        source = sys.stdin
        close_source = False
    else:
        try:
            source = Path(args.file).open(encoding="utf-8")
        except OSError as exc:
            _log(f"can't read Aplomado events: {exc}")
            return 2
        close_source = True

    seen = 0
    failed = False
    try:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            seen += 1
            try:
                event = parse_aplomado_event(json.loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                _log(f"invalid Aplomado event on line {line_number}: {exc}")
                failed = True
                continue
            if not _route_event(args, event):
                _log(f"duplicate event {event.id}")
                failed = True
                continue
            print(event.id)
    finally:
        if close_source:
            source.close()
    if not seen:
        _log("no Aplomado events found")
        return 2
    return 1 if failed else 0


def cmd_serve(args) -> int:
    """Run the webchat server."""
    from .web import create_app
    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


_DISPATCH = {
    "spawn": cmd_spawn,
    "list": cmd_list,
    "chat": cmd_chat,
    "run": cmd_run,
    "wake": cmd_wake,
    "forget": cmd_forget,
    "event": cmd_event,
    "events": cmd_events,
    "rule-add": cmd_rule_add,
    "rule-list": cmd_rule_list,
    "rule-remove": cmd_rule_remove,
    "ingest-aplomado": cmd_ingest_aplomado,
    "serve": cmd_serve,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
