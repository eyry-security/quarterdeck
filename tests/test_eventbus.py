"""EventBus: subscribe/publish, unsubscribes, handler isolation."""

from __future__ import annotations

from quarterdeck.events import CHAT_MESSAGE, TICK, WEBHOOK, Event, EventBus


def test_publish_delivers_to_subscribers():
    bus = EventBus()
    got = []
    bus.subscribe(TICK, got.append)
    bus.publish(Event(type=TICK, payload={"ts": 1.0}))
    assert len(got) == 1
    assert got[0].type == TICK
    assert got[0].payload == {"ts": 1.0}
    assert got[0].ts > 0


def test_event_types_are_isolated():
    bus = EventBus()
    ticks, chats = [], []
    bus.subscribe(TICK, ticks.append)
    bus.subscribe(CHAT_MESSAGE, chats.append)
    bus.publish(Event(type=WEBHOOK, payload={}))
    assert ticks == [] and chats == []


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    got = []
    unsub = bus.subscribe(TICK, got.append)
    bus.publish(Event(type=TICK))
    unsub()
    bus.publish(Event(type=TICK))
    assert len(got) == 1


def test_multiple_subscribers_all_called():
    bus = EventBus()
    order = []
    bus.subscribe(TICK, lambda e: order.append("a"))
    bus.subscribe(TICK, lambda e: order.append("b"))
    bus.publish(Event(type=TICK))
    assert order == ["a", "b"]


def test_bad_handler_does_not_kill_bus():
    bus = EventBus()

    def boom(e):
        raise RuntimeError("handler bug")

    good = []
    bus.subscribe(TICK, boom)
    bus.subscribe(TICK, good.append)
    bus.publish(Event(type=TICK))  # must not raise
    assert len(good) == 1
