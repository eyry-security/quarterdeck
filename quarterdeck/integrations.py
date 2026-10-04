"""Schema-tolerant adapters for events from other Eyry components."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from .events import APLOMADO_SCAN_COMPLETED, Event

_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_APLOMADO_FIELDS = {"type", "id", "producer", "timestamp", "data"}


def _validate_aplomado_data(envelope: object) -> dict:
    if not isinstance(envelope, dict):
        raise ValueError("Aplomado envelope must be an object")
    for field in ("target", "summary", "scanned_at", "findings"):
        if field not in envelope:
            raise ValueError(f"Aplomado envelope missing {field!r}")
    if not isinstance(envelope["target"], str) or not envelope["target"].strip():
        raise ValueError("Aplomado target must be a non-empty string")
    if not isinstance(envelope["findings"], list):
        raise ValueError("Aplomado findings must be an array")
    if "ok" in envelope and not isinstance(envelope["ok"], bool):
        raise ValueError("Aplomado ok must be boolean when present")
    if "metadata" in envelope and not isinstance(envelope["metadata"], dict):
        raise ValueError("Aplomado metadata must be an object when present")
    for finding in envelope["findings"]:
        if not isinstance(finding, dict):
            raise ValueError("each Aplomado finding must be an object")
        severity = str(finding.get("severity", "")).lower()
        if severity not in _SEVERITIES:
            raise ValueError(f"invalid Aplomado severity {severity!r}")
    return dict(envelope)


def aplomado_event(envelope: dict, *, event_id: str | None = None) -> Event:
    """Wrap an Aplomado report without depending on its Python package."""
    envelope = _validate_aplomado_data(envelope)
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    stable_id = event_id or "aplomado-" + hashlib.sha256(canonical.encode()).hexdigest()
    return Event(
        id=stable_id,
        type=APLOMADO_SCAN_COMPLETED,
        source="aplomado",
        payload={"data": envelope},
    )


def parse_aplomado_event(document: object) -> Event:
    """Consume Aplomado's producer envelope while preserving future fields."""
    if not isinstance(document, dict):
        raise ValueError("Aplomado event must be an object")
    if document.get("type") != APLOMADO_SCAN_COMPLETED:
        raise ValueError(f"unexpected Aplomado event type {document.get('type')!r}")
    event_id = document.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Aplomado event id must be a non-empty string")
    producer = document.get("producer")
    if producer != "aplomado":
        raise ValueError("Aplomado event producer must be 'aplomado'")
    stamp = document.get("timestamp")
    if not isinstance(stamp, str):
        raise ValueError("Aplomado event timestamp must be ISO-8601 text")
    candidate = stamp[:-1] + "+00:00" if stamp.endswith(("Z", "z")) else stamp
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("Aplomado event timestamp must be ISO-8601 text") from exc
    if parsed.tzinfo is None:
        raise ValueError("Aplomado event timestamp must include a timezone")
    data = _validate_aplomado_data(document.get("data"))
    payload = {"data": data, "producer_timestamp": stamp}
    extras = {key: value for key, value in document.items() if key not in _APLOMADO_FIELDS}
    if extras:
        payload["producer_fields"] = extras
    return Event(
        id=event_id,
        type=APLOMADO_SCAN_COMPLETED,
        source="aplomado",
        ts=parsed.astimezone(timezone.utc).timestamp(),
        payload=payload,
    )


def format_aplomado_alert(envelope: dict) -> str:
    findings = envelope.get("findings") or []
    counts = Counter(str(item.get("severity", "info")).lower() for item in findings)
    summary = ", ".join(
        f"{counts[level]} {level}"
        for level in ("critical", "high", "medium", "low", "info")
        if counts[level]
    ) or "no findings"
    lines = [
        f"🛰 Aplomado reviewed {envelope.get('target', 'unknown target')}: {summary}.",
    ]
    if envelope.get("summary"):
        lines.append(str(envelope["summary"]))
    for finding in findings:
        if str(finding.get("severity", "")).lower() in {"critical", "high"}:
            lines.append(
                f"[{str(finding['severity']).upper()}] {finding.get('title', 'untitled')}"
            )
    return "\n".join(lines)
