"""Typed durable event history and deduplication."""

from quarterdeck.events import Event, EventBus, EventStore, TICK


def test_event_roundtrip_preserves_schema_fields():
    event = Event(type="host.probed", payload={"host": "a.example"},
                  id="evt-1", source="vedette", schema_version=1, ts=12.5)
    restored = Event.from_dict(event.to_dict())
    assert restored == event


def test_store_persists_deduplicates_and_reloads(tmp_path):
    store = EventStore(tmp_path)
    event = Event(type=TICK, id="same-id", payload={"n": 1})
    assert store.append(event) is True
    assert store.append(event) is False
    reloaded = EventStore(tmp_path)
    assert reloaded.append(event) is False
    assert reloaded.history()[0].payload == {"n": 1}


def test_bus_commits_before_delivery_and_skips_duplicate(tmp_path):
    store = EventStore(tmp_path)
    bus = EventBus(store=store)
    seen = []
    bus.subscribe(
        "notice", lambda event: seen.append((event.id, len(store.history()))),
        subscriber_id="test.seen",
    )
    event = Event(type="notice", id="evt-1")
    assert bus.publish(event) is True
    assert seen == [("evt-1", 1)]
    assert bus.publish(event) is False
    assert seen == [("evt-1", 1)]


def test_history_filters_and_limits(tmp_path):
    store = EventStore(tmp_path)
    store.append(Event(type="a", id="1"))
    store.append(Event(type="b", id="2"))
    store.append(Event(type="a", id="3"))
    assert [event.id for event in store.history(event_type="a", limit=1)] == ["3"]


def test_unsupported_schema_version_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        Event(type="future", schema_version=2)
    with pytest.raises(ValueError, match="unsupported"):
        Event.from_dict({"type": "future", "schema_version": 2})
    with pytest.raises(ValueError, match="unsupported"):
        Event.from_dict({"type": "future", "schema_version": "1"})
    with pytest.raises(ValueError, match="unsupported"):
        Event.from_dict({"type": "future", "schema_version": True})


def test_two_store_instances_claim_duplicate_once(tmp_path):
    import threading

    stores = [EventStore(tmp_path), EventStore(tmp_path)]
    barrier = threading.Barrier(2)
    results = []

    def append(store):
        barrier.wait()
        results.append(store.append(Event(type="notice", id="shared")))

    threads = [threading.Thread(target=append, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(results) == [False, True]
    assert [event.id for event in stores[0].history()] == ["shared"]


def test_failed_handler_is_recorded_and_replayable(tmp_path):
    store = EventStore(tmp_path)
    bus = EventBus(store=store)
    attempts = []
    stable = []

    def flaky(event):
        attempts.append(event.id)
        if len(attempts) == 1:
            raise RuntimeError("temporary")

    bus.subscribe("notice", flaky, subscriber_id="test.flaky")
    bus.subscribe(
        "notice", lambda event: stable.append(event.id), subscriber_id="test.stable"
    )
    assert bus.publish(Event(type="notice", id="evt-retry")) is True
    assert bus.replay("evt-retry") is True
    assert attempts == ["evt-retry", "evt-retry"]
    assert stable == ["evt-retry"]
    records = [__import__("json").loads(line) for line in
               store.delivery_path.read_text().splitlines()]
    assert [record["status"] for record in records] == [
        "running", "failed", "running", "succeeded", "running", "succeeded"
    ]


def _process_append_event(home, start, results):
    start.wait()
    results.put(EventStore(home).append(Event(type="notice", id="process-shared")))


def test_event_id_claim_is_process_safe(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_process_append_event, args=(tmp_path, start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    values = [results.get(timeout=2) for _ in processes]
    for process in processes:
        process.join(timeout=2)
        assert process.exitcode == 0
    assert sorted(values) == [False, True]


def test_replay_uses_stable_subscriber_identity_after_topology_change(tmp_path):
    store = EventStore(tmp_path)
    first = EventBus(store=store)
    calls = []
    first.subscribe("notice", lambda event: None, subscriber_id="stable.success")

    def fail(event):
        raise RuntimeError("temporary")

    first.subscribe("notice", fail, subscriber_id="stable.failed")
    assert first.publish(Event(type="notice", id="topology")) is True

    restarted = EventBus(store=EventStore(tmp_path))
    restarted.subscribe(
        "notice", lambda event: calls.append(event.id), subscriber_id="stable.failed"
    )
    assert restarted.replay("topology") is True
    assert calls == ["topology"]


def _process_replay(home, start):
    store = EventStore(home)
    bus = EventBus(store=store)

    def side_effect(event):
        with open(str(home / "effects.txt"), "a", encoding="utf-8") as handle:
            handle.write(event.id + "\n")

    bus.subscribe("notice", side_effect, subscriber_id="stable.effect")
    start.wait()
    bus.replay("concurrent-replay")


def test_replay_delivery_claim_is_process_safe(tmp_path):
    import multiprocessing

    event = Event(type="notice", id="concurrent-replay")
    store = EventStore(tmp_path)
    store.append(event)
    store.record_delivery(event, "stable.effect", "failed", "temporary")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(target=_process_replay, args=(tmp_path, start))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=2)
        assert process.exitcode == 0
    assert (tmp_path / "effects.txt").read_text().splitlines() == ["concurrent-replay"]


def test_event_required_field_types_are_strict():
    import pytest

    for value in (
        {"type": 3, "id": "x"},
        {"type": "x", "id": 3},
        {"type": "x", "id": "x", "source": {}},
        {"type": "x", "id": "x", "ts": "12.5"},
        {"type": "x", "id": "x", "payload": []},
    ):
        with pytest.raises(ValueError):
            Event.from_dict(value)
