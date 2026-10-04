"""Schema-tolerant adapters for events from other Eyry components."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .events import APLOMADO_SCAN_COMPLETED, Event

_SEVERITIES = {"critical", "high", "medium", "low", "info"}


def aplomado_event(envelope: dict, *, event_id: str | None = None) -> Event:
    """Wrap an Aplomado report without depending on its Python package."""
    if not isinstance(envelope, dict):
        raise ValueError("Aplomado envelope must be an object")
    for field in ("target", "summary", "scanned_at", "findings"):
        if field not in envelope:
            raise ValueError(f"Aplomado envelope missing {field!r}")
    if not isinstance(envelope["target"], str) or not envelope["target"].strip():
        raise ValueError("Aplomado target must be a non-empty string")
    if not isinstance(envelope["findings"], list):
        raise ValueError("Aplomado findings must be an array")
    for finding in envelope["findings"]:
        if not isinstance(finding, dict):
            raise ValueError("each Aplomado finding must be an object")
        severity = str(finding.get("severity", "")).lower()
        if severity not in _SEVERITIES:
            raise ValueError(f"invalid Aplomado severity {severity!r}")
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    stable_id = event_id or "aplomado-" + hashlib.sha256(canonical.encode()).hexdigest()
    return Event(
        id=stable_id,
        type=APLOMADO_SCAN_COMPLETED,
        source="aplomado",
        payload={"data": envelope},
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
