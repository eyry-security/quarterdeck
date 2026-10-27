"""Eyry Pro platform tools for the captain agent.

Thin wrappers over Eyry Pro's HTTP API. Quarterdeck never touches Pro's
database directly: every call goes through the service-token credential on
the configured loopback base URL. Tool errors are returned as readable
chat-ready strings, never raised into the agent loop.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from langchain_core.tools import StructuredTool

DEFAULT_PRO_API_URL = "http://127.0.0.1:8431"
PRO_API_URL_ENV = "QD_PRO_API_URL"
SERVICE_TOKEN_ENV = "QD_SERVICE_TOKEN"
CAPTAIN_ACTOR = "self via quarterdeck"
DEFAULT_TIMEOUT = 15

CADENCES = ("off", "hourly", "6h", "daily", "weekly")
SEVERITIES = ("info", "low", "medium", "high", "critical")
STATUSES = ("open", "pending_recheck", "closed", "informative")


class ProAPIError(Exception):
    """A readable failure talking to Eyry Pro."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ProClient:
    """Minimal JSON client for the Eyry Pro API (stdlib only)."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT, opener: Callable | None = None):
        self.base_url = (base_url or os.environ.get(PRO_API_URL_ENV)
                         or DEFAULT_PRO_API_URL).rstrip("/")
        self.token = (token if token is not None
                      else os.environ.get(SERVICE_TOKEN_ENV, "")).strip()
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        headers = {
            "Accept": "application/json",
            "User-Agent": "quarterdeck-captain",
            "X-Eyry-Actor": CAPTAIN_ACTOR,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            raise ProAPIError(
                f"Eyry Pro returned {exc.code}: {detail}", status=exc.code)
        except urllib.error.URLError as exc:
            raise ProAPIError(
                f"cannot reach Eyry Pro at {self.base_url}: {exc.reason}")
        except Exception as exc:  # noqa: BLE001 — network/transport failures
            raise ProAPIError(f"Eyry Pro request failed: {exc}")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ProAPIError("Eyry Pro returned a non-JSON response")

    # -- reads ----------------------------------------------------------
    def list_scopes(self) -> list:
        return self.request("GET", "/api/scopes/summary").get("scopes", [])

    def list_hosts(self, scope_id: int, limit: int = 50,
                   offset: int = 0) -> dict:
        return self.request("GET", f"/api/scopes/{scope_id}/hosts",
                            params={"limit": limit, "offset": offset})

    def list_scans(self, scope_id: int, limit: int = 20) -> list:
        return self.request("GET", f"/api/scopes/{scope_id}/scans",
                            params={"limit": limit}).get("scans", [])

    def get_scan(self, scan_id) -> dict:
        sid = urllib.parse.quote(str(scan_id), safe="")
        return self.request("GET", f"/api/scans/{sid}")

    def get_schedule(self, scope_id: int) -> dict:
        return self.request("GET", f"/api/scopes/{scope_id}/schedule")

    def get_finding(self, finding_id: int) -> dict:
        return self.request("GET", f"/api/findings/{finding_id}")

    def finding_events(self, limit: int = 500) -> list:
        return self.request("GET", "/api/findings/events",
                            params={"limit": limit}).get("events", [])

    def findings_for_scope(self, scope_id: int, limit: int = 500,
                           offset: int = 0) -> dict:
        return self.request("GET", f"/api/scopes/{scope_id}/findings",
                            params={"limit": limit, "offset": offset})

    def findings_all(self, limit: int = 500, offset: int = 0,
                     severity: str = "", status: str = "") -> dict:
        return self.request("GET", "/api/findings", params={
            "limit": limit, "offset": offset,
            "severity": severity or None, "status": status or None,
        })

    # -- mutations ------------------------------------------------------
    def start_scan(self, scope_id: int) -> dict:
        return self.request("POST", f"/api/scopes/{scope_id}/scan")

    def set_schedule(self, scope_id: int, cadence: str) -> dict:
        return self.request("PUT", f"/api/scopes/{scope_id}/schedule",
                            body={"cadence": cadence})

    def verify_fix(self, finding_id: int) -> dict:
        return self.request("POST", f"/api/findings/{finding_id}/verify-fix")

    def set_finding_status(self, finding_id: int, status: str,
                           closed_reason: str = "", note: str = "") -> dict:
        return self.request("PATCH", f"/api/findings/{finding_id}/status",
                            body={"status": status,
                                  "closed_reason": closed_reason,
                                  "note": note})

    def record_activity(self, action: str, target: str = "",
                        detail: str = "") -> dict:
        return self.request("POST", "/api/activity", body={
            "action": action, "target": target, "detail": detail,
            "actor": CAPTAIN_ACTOR,
        })


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    if text:
        try:
            payload = json.loads(text)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, list):
                return json.dumps(detail)
            if detail:
                return str(detail)
        except ValueError:
            pass
        return text[:500]
    return exc.reason or "request failed"


def _make_tool(name: str, description: str, func: Callable) -> StructuredTool:
    return StructuredTool.from_function(func=func, name=name,
                                        description=description)


def _as_int(value, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ProAPIError(f"{label} must be a number")


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "on", "enabled")


def _when(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError):
        return str(value)


def _resolve_scope(client, scope) -> dict:
    raw = str(scope or "").strip()
    if not raw:
        raise ProAPIError("scope is required")
    scopes = client.list_scopes()
    if raw.isdigit():
        wanted = int(raw)
        for item in scopes:
            try:
                if int(item.get("id")) == wanted:
                    return item
            except (TypeError, ValueError):
                continue
    lowered = raw.lower()
    for item in scopes:
        if str(item.get("pattern", "")).lower() == lowered:
            return item
    raise ProAPIError(f"scope {raw!r} not found; use list_scopes first")


def _format_scope(item: dict) -> str:
    status = "verified" if item.get("verified") else (
        "verification needed" if item.get("verification_required")
        else "unverified")
    last = item.get("last_scan") or {}
    scan_state = "-"
    if last:
        scan_state = (f"#{last.get('id')} {last.get('status', '?')}"
                      f" stage={last.get('stage') or '-'}")
    return (f"#{item.get('id')} {item.get('pattern')} "
            f"[mode={item.get('mode') or '-'}, {status}] "
            f"cadence={item.get('scan_cadence') or 'off'}, "
            f"hosts={item.get('hosts_count', 0)}, "
            f"findings={item.get('findings_count', 0)}, "
            f"last_scan={scan_state}")


def _format_scan(scan: dict) -> str:
    bits = [
        f"{scan.get('run_id') or scan.get('id')}",
        f"{scan.get('kind') or 'full'}",
        f"status={scan.get('status') or '?'}",
        f"stage={scan.get('stage') or '-'}",
        f"hosts={scan.get('hosts_probed', 0)}/{scan.get('hosts_found', 0)}",
        f"findings={scan.get('findings_count', 0)}",
        f"started={_when(scan.get('created_at'))}",
    ]
    if scan.get("finished_at"):
        bits.append(f"finished={_when(scan.get('finished_at'))}")
    if scan.get("error"):
        bits.append(f"error={str(scan['error'])[:200]}")
    return " ".join(bits)


def _format_finding(item: dict) -> str:
    return (f"#{item.get('id')} [{item.get('severity', '?')}] "
            f"{item.get('host', '?')} — {item.get('title', '?')} "
            f"(status={item.get('status', '?')})")


def platform_tools(agent_name: str, home=None, client=None) -> list:
    """Build the Eyry Pro platform tools bound to the captain.

    ``client`` is injectable for tests; production builds a service-token
    client from ``QD_PRO_API_URL`` / ``QD_SERVICE_TOKEN``.
    """
    client = client or ProClient()

    def _audit(action: str, target: str, detail: str = "") -> str:
        try:
            client.record_activity(action, target, detail)
            return ""
        except Exception as exc:  # noqa: BLE001 — mutation already succeeded
            return f" (audit warning: {exc})"

    def list_scopes() -> str:
        """List recon scopes with mode, verification, cadence and scan state."""
        try:
            scopes = client.list_scopes()
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if not scopes:
            return "no scopes configured"
        return "scopes:\n" + "\n".join(_format_scope(s) for s in scopes)

    def list_hosts(scope: str, limit: int = 50, offset: int = 0) -> str:
        """List discovered hosts for a scope. Args: scope (id or pattern), limit, offset."""
        try:
            scope_item = _resolve_scope(client, scope)
            data = client.list_hosts(scope_item["id"],
                                     limit=max(1, min(int(limit), 500)),
                                     offset=max(0, int(offset)))
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        hosts = data.get("hosts", [])
        if not hosts:
            return f"{scope_item['pattern']}: no hosts discovered"
        lines = []
        for host in hosts:
            probed = _when(host.get("last_probed_at"))
            detail = host.get("probe_data") or ""
            if detail:
                detail = f" {str(detail)[:180]}"
            lines.append(f"{host.get('host')} (last_probed={probed}){detail}")
        return (f"{scope_item['pattern']} hosts "
                f"({data.get('total', len(hosts))} total):\n"
                + "\n".join(lines))

    def scan_status(scope: str) -> str:
        """Show latest scan runs and live stage/progress for a scope. Args: scope."""
        try:
            scope_item = _resolve_scope(client, scope)
            scans = client.list_scans(scope_item["id"], limit=10)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if not scans:
            return f"{scope_item['pattern']}: no scans yet"
        latest = scans[0]
        live = None
        try:
            live = client.get_scan(latest.get("run_id") or latest.get("id"))
        except Exception:  # noqa: BLE001 — list rows are still useful
            live = None
        lines = [_format_scan(scan) for scan in scans[:5]]
        if live:
            lines.insert(0, "live: " + _format_scan(live))
        return f"{scope_item['pattern']} scans:\n" + "\n".join(lines)

    def list_findings(scope: str = "", host: str = "", severity: str = "",
                      status: str = "", limit: int = 20,
                      offset: int = 0) -> str:
        """List findings (summary only). Args: scope (id/pattern), host, severity, status, limit, offset."""
        sev = (severity or "").strip().lower()
        st = (status or "").strip().lower()
        host_filter = (host or "").strip().lower()
        if sev and sev not in SEVERITIES:
            return f"error: severity must be one of {', '.join(SEVERITIES)}"
        if st and st not in STATUSES:
            return f"error: status must be one of {', '.join(STATUSES)}"
        try:
            if scope:
                scope_item = _resolve_scope(client, scope)
                data = client.findings_for_scope(scope_item["id"], limit=500)
                label = scope_item["pattern"]
            else:
                data = client.findings_all(limit=500)
                label = "all scopes"
            findings = list(data.get("findings", []))
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if sev:
            findings = [f for f in findings
                        if str(f.get("severity", "")).lower() == sev]
        if st:
            findings = [f for f in findings
                        if str(f.get("status", "")).lower() == st]
        if host_filter:
            findings = [f for f in findings
                        if host_filter in str(f.get("host", "")).lower()]
        total = len(findings)
        try:
            start = max(0, _as_int(offset, "offset"))
            count = max(1, min(_as_int(limit, "limit"), 200))
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        page = findings[start:start + count]
        if not page:
            return f"{label}: no findings match"
        return (f"{label} findings ({start + 1}-{start + len(page)} of {total}):\n"
                + "\n".join(_format_finding(f) for f in page))

    def get_finding(finding_id: int) -> str:
        """Full finding: summary, reproduction, impact, remediation, evidence, history. Args: finding id."""
        try:
            fid = _as_int(finding_id, "finding id")
            item = client.get_finding(fid)
            events = client.finding_events(limit=500)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        history = [e for e in events
                   if str(e.get("finding_id")) == str(fid)]
        repro = item.get("repro_steps") or []
        lines = [
            f"#{item.get('id')} [{item.get('severity')}] "
            f"{item.get('host')} — {item.get('title')}",
            f"status: {item.get('status')} (changed {_when(item.get('status_changed_at'))})",
            f"scope: {item.get('scope_pattern') or item.get('scope_id')}",
            f"source: {item.get('source') or '-'} first seen: {_when(item.get('created_at'))}",
            f"risk type: {item.get('risk_type') or '-'}",
            "",
            "summary:",
            item.get("summary") or "(none)",
            "",
            "reproduction:",
        ]
        lines.extend(f"- {step}" for step in repro) if repro else lines.append("(none)")
        lines += [
            "",
            "impact:",
            item.get("impact") or "(none)",
            "",
            "remediation:",
            item.get("remediation") or "(none)",
            "",
            "evidence:",
            f"detail: {item.get('detail') or '(none)'}",
            f"verify evidence: {item.get('verify_evidence') or '(none)'}",
            f"closed reason: {item.get('closed_reason') or '-'}",
            "",
            "history:",
        ]
        if history:
            for event in history:
                lines.append(
                    f"- {_when(event.get('created_at'))}: "
                    f"{event.get('old_status') or 'new'} -> "
                    f"{event.get('new_status')}"
                    + (f" ({event.get('reason')})" if event.get("reason") else ""))
        else:
            lines.append("(no status events)")
        return "\n".join(lines)

    def start_scan(scope: str) -> str:
        """Enqueue a scan for a scope, refusing when one is already running. Args: scope."""
        try:
            scope_item = _resolve_scope(client, scope)
            for scan in client.list_scans(scope_item["id"], limit=10):
                if str(scan.get("status", "")).lower() in ("pending", "running"):
                    return (f"error: a scan is already running for "
                            f"{scope_item['pattern']} "
                            f"({scan.get('run_id') or scan.get('id')}, "
                            f"{scan.get('status')})")
            result = client.start_scan(scope_item["id"])
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        run_id = result.get("run_id") or result.get("scan_id") or result.get("id")
        warning = _audit("start_scan", scope_item["pattern"],
                         f"run_id={run_id}")
        return (f"scan queued for {scope_item['pattern']}: run {run_id} "
                f"({result.get('status', 'pending')}){warning}")

    def list_schedules(scope: str = "") -> str:
        """List scan schedules for one scope, or all scopes. Args: scope (optional)."""
        try:
            if scope:
                items = [_resolve_scope(client, scope)]
            else:
                items = client.list_scopes()
            if not items:
                return "no scopes configured"
            lines = []
            for item in items:
                schedule = client.get_schedule(item["id"])
                cadence = schedule.get("cadence") or "off"
                enabled = cadence != "off"
                lines.append(
                    f"#{item['id']} {item['pattern']}: cadence={cadence} "
                    f"enabled={'yes' if enabled else 'no'} "
                    f"next={_when(schedule.get('next_scan_at'))}")
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        return "schedules:\n" + "\n".join(lines)

    def set_schedule(scope: str, cadence: str = "",
                     enabled: bool | None = None) -> str:
        """Set a scope's scan schedule. Args: scope, cadence (off/hourly/6h/daily/weekly), enabled (optional)."""
        try:
            scope_item = _resolve_scope(client, scope)
            current = client.get_schedule(scope_item["id"])
            current_cadence = current.get("cadence") or "off"
            requested = (cadence or "").strip().lower()
            if requested and requested not in CADENCES:
                return f"error: cadence must be one of {', '.join(CADENCES)}"
            if enabled is None:
                target = requested or current_cadence
                if target not in CADENCES:
                    return f"error: cadence must be one of {', '.join(CADENCES)}"
            elif not _as_bool(enabled):
                target = "off"
            else:
                target = requested if requested and requested != "off" else (
                    current_cadence if current_cadence != "off" else "daily")
            schedule = client.set_schedule(scope_item["id"], target)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        warning = _audit("set_schedule", scope_item["pattern"],
                         f"cadence={schedule.get('cadence', target)}")
        return (f"schedule for {scope_item['pattern']}: "
                f"cadence={schedule.get('cadence', target)}, "
                f"next={_when(schedule.get('next_scan_at'))}{warning}")

    def recheck_finding(finding_id: int) -> str:
        """Enqueue the existing recheck/fix-verification flow for a finding. Args: finding id."""
        try:
            fid = _as_int(finding_id, "finding id")
            result = client.verify_fix(fid)
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        run_id = result.get("run_id") or result.get("scan_id") or result.get("id")
        warning = _audit("recheck_finding", f"finding:{fid}",
                         f"run_id={run_id or result.get('status', '')}")
        if run_id:
            return f"recheck queued for finding #{fid}: run {run_id}{warning}"
        return (f"recheck for finding #{fid}: "
                f"{result.get('status', 'queued')}{warning}")

    def resolve_finding(finding_id: int, note: str = "") -> str:
        """Close a finding as resolved with an optional note. Args: finding id, note."""
        try:
            fid = _as_int(finding_id, "finding id")
            result = client.set_finding_status(
                fid, "closed", closed_reason="false_positive", note=note or "")
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        warning = _audit("resolve_finding", f"finding:{fid}", note or "")
        return (f"finding #{fid} closed (status={result.get('status', 'closed')})"
                f"{warning}")

    def reopen_finding(finding_id: int) -> str:
        """Reopen a closed or informative finding. Args: finding id."""
        try:
            fid = _as_int(finding_id, "finding id")
            result = client.set_finding_status(fid, "open")
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        warning = _audit("reopen_finding", f"finding:{fid}")
        return (f"finding #{fid} reopened (status={result.get('status', 'open')})"
                f"{warning}")

    def mark_informative(finding_id: int, note: str = "") -> str:
        """Mark a finding informative (acknowledged, don't care). Args: finding id, optional note."""
        try:
            fid = _as_int(finding_id, "finding id")
            result = client.set_finding_status(fid, "informative",
                                               note=note or "")
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        warning = _audit("mark_informative", f"finding:{fid}", note or "")
        return (f"finding #{fid} marked informative "
                f"(status={result.get('status', 'informative')}){warning}")

    def unmark_informative(finding_id: int) -> str:
        """Return an informative finding to open. Args: finding id."""
        try:
            fid = _as_int(finding_id, "finding id")
            result = client.set_finding_status(fid, "open")
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        warning = _audit("unmark_informative", f"finding:{fid}")
        return (f"finding #{fid} returned to open "
                f"(status={result.get('status', 'open')}){warning}")

    return [
        _make_tool(
            "list_scopes",
            "List Eyry Pro recon scopes with mode, verification, cadence, "
            "host/finding counts and latest scan state.",
            list_scopes,
        ),
        _make_tool(
            "list_hosts",
            "List discovered hosts for a scope from Eyry Pro's Hosts data. "
            "Args: scope (id or pattern), limit (default 50), offset.",
            list_hosts,
        ),
        _make_tool(
            "scan_status",
            "Show the latest scan runs for a scope plus live stage/progress. "
            "Args: scope (id or pattern).",
            scan_status,
        ),
        _make_tool(
            "list_findings",
            "List findings with summary fields only, paginated. Args: scope "
            "(optional id/pattern), host, severity, status, limit, offset.",
            list_findings,
        ),
        _make_tool(
            "get_finding",
            "Dive deep on one finding: summary, reproduction, impact, "
            "remediation, evidence and status history. Args: finding id.",
            get_finding,
        ),
        _make_tool(
            "start_scan",
            "Enqueue a scan for a scope via Eyry Pro's queue API. Refuses if "
            "a scan is already running. Args: scope (id or pattern).",
            start_scan,
        ),
        _make_tool(
            "list_schedules",
            "View scan scheduler entries for one scope or all scopes. "
            "Args: scope (optional id/pattern).",
            list_schedules,
        ),
        _make_tool(
            "set_schedule",
            "Manage a scope's scan schedule. Args: scope (id/pattern), "
            "cadence (off/hourly/6h/daily/weekly), enabled (optional).",
            set_schedule,
        ),
        _make_tool(
            "recheck_finding",
            "Enqueue the existing recheck/fix-verification flow for a finding. "
            "Args: finding id.",
            recheck_finding,
        ),
        _make_tool(
            "resolve_finding",
            "Close a finding as resolved through the existing lifecycle. "
            "Args: finding id, resolution note.",
            resolve_finding,
        ),
        _make_tool(
            "reopen_finding",
            "Reopen a closed or informative finding. Args: finding id.",
            reopen_finding,
        ),
        _make_tool(
            "mark_informative",
            "Set a finding's disposition to informative (acknowledged, don't "
            "care). Args: finding id, optional note.",
            mark_informative,
        ),
        _make_tool(
            "unmark_informative",
            "Return an informative finding to open. Args: finding id.",
            unmark_informative,
        ),
    ]
