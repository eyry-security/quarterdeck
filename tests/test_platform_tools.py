"""Captain's Eyry Pro platform tools against a mocked Pro API."""
from __future__ import annotations

import pytest

from quarterdeck.platform_tools import ProAPIError, platform_tools


class FakePro:
    def __init__(self):
        self.fail: set[str] = set()
        self.calls: list[tuple] = []
        self.activities: list[dict] = []
        self.scopes = [{
            "id": 1,
            "pattern": "*.example.com",
            "mode": "ownership",
            "verified": True,
            "verification_required": False,
            "scan_cadence": "daily",
            "hosts_count": 2,
            "findings_count": 2,
            "last_scan": {"id": 10, "status": "complete", "stage": "complete"},
        }]
        self.hosts = {
            "hosts": [{
                "host": "a.example.com",
                "first_seen": 1000,
                "last_probed_at": 2000,
                "probe_data": '{"ports": [443]}',
            }],
            "total": 1,
        }
        self.scans = [{
            "id": 10,
            "run_id": "run-10",
            "kind": "full",
            "status": "running",
            "stage": "probing",
            "hosts_found": 2,
            "hosts_probed": 1,
            "findings_count": 0,
            "created_at": 1000,
        }]
        self.findings = [
            {"id": 7, "scope_id": 1, "scope_pattern": "*.example.com",
             "host": "a.example.com", "severity": "high",
             "title": "Missing security header", "status": "open",
             "detail": "hidden list detail", "created_at": 1000},
            {"id": 8, "scope_id": 1, "scope_pattern": "*.example.com",
             "host": "b.example.com", "severity": "low",
             "title": "Server version leak", "status": "closed",
             "detail": "hidden list detail", "created_at": 900},
        ]
        self.events = [{
            "id": 1, "finding_id": 7, "old_status": "open",
            "new_status": "informative", "reason": "known",
            "created_at": 1200,
        }]
        self.schedule = {"cadence": "daily", "next_scan_at": 5000}

    def _check(self, name):
        if name in self.fail:
            raise ProAPIError(f"{name} failed: mocked error")

    def list_scopes(self):
        self.calls.append(("list_scopes",))
        self._check("list_scopes")
        return [dict(s) for s in self.scopes]

    def list_hosts(self, scope_id, limit=50, offset=0):
        self.calls.append(("list_hosts", scope_id, limit, offset))
        self._check("list_hosts")
        return dict(self.hosts)

    def list_scans(self, scope_id, limit=20):
        self.calls.append(("list_scans", scope_id, limit))
        self._check("list_scans")
        return [dict(s) for s in self.scans]

    def get_scan(self, scan_id):
        self.calls.append(("get_scan", scan_id))
        self._check("get_scan")
        return dict(self.scans[0], id=scan_id)

    def get_schedule(self, scope_id):
        self.calls.append(("get_schedule", scope_id))
        self._check("get_schedule")
        return dict(self.schedule)

    def get_finding(self, finding_id):
        self.calls.append(("get_finding", finding_id))
        self._check("get_finding")
        found = next(f for f in self.findings if f["id"] == finding_id)
        return {**found,
                "summary": "A missing header weakens transport security.",
                "repro_steps": ["curl -I https://a.example.com"],
                "impact": "Downgrade and injection risk.",
                "remediation": "Send the missing header.",
                "risk_type": "headers",
                "status_changed_at": 1100,
                "verify_evidence": "header still absent",
                "closed_reason": ""}

    def finding_events(self, limit=500):
        self.calls.append(("finding_events", limit))
        self._check("finding_events")
        return [dict(e) for e in self.events]

    def findings_for_scope(self, scope_id, limit=500, offset=0):
        self.calls.append(("findings_for_scope", scope_id, limit, offset))
        self._check("findings_for_scope")
        return {"findings": [dict(f) for f in self.findings], "total": 2}

    def findings_all(self, limit=500, offset=0, severity="", status=""):
        self.calls.append(("findings_all", limit, offset, severity, status))
        self._check("findings_all")
        return {"findings": [dict(f) for f in self.findings], "total": 2}

    def start_scan(self, scope_id):
        self.calls.append(("start_scan", scope_id))
        self._check("start_scan")
        return {"scan_id": "run-11", "run_id": "run-11", "status": "pending"}

    def set_schedule(self, scope_id, cadence):
        self.calls.append(("set_schedule", scope_id, cadence))
        self._check("set_schedule")
        return {"cadence": cadence, "next_scan_at": 6000}

    def verify_fix(self, finding_id):
        self.calls.append(("verify_fix", finding_id))
        self._check("verify_fix")
        return {"run_id": "run-12", "status": "pending"}

    def set_finding_status(self, finding_id, status, closed_reason="",
                           note=""):
        self.calls.append(("set_finding_status", finding_id, status,
                           closed_reason, note))
        self._check("set_finding_status")
        return {"id": finding_id, "status": status}

    def record_activity(self, action, target="", detail=""):
        self.calls.append(("record_activity", action, target, detail))
        self._check("record_activity")
        self.activities.append({"action": action, "target": target,
                                "detail": detail})


@pytest.fixture()
def fake():
    return FakePro()


@pytest.fixture()
def tools(fake):
    return {tool.name: tool for tool in
            platform_tools("self", client=fake)}


def _call(tool, **kwargs):
    return tool.invoke(kwargs)


def test_list_scopes(tools):
    out = _call(tools["list_scopes"])
    assert "*.example.com" in out
    assert "mode=ownership" in out
    assert "cadence=daily" in out


def test_list_hosts_by_pattern(tools, fake):
    out = _call(tools["list_hosts"], scope="*.example.com")
    assert "a.example.com" in out
    assert ("list_hosts", 1, 50, 0) in fake.calls


def test_scan_status_includes_live_progress(tools, fake):
    out = _call(tools["scan_status"], scope="1")
    assert "live:" in out
    assert "stage=probing" in out
    assert ("get_scan", "run-10") in fake.calls


def test_list_findings_summary_and_filters(tools):
    out = _call(tools["list_findings"], scope="*.example.com",
                severity="high", host="a.example")
    assert "#7" in out
    assert "#8" not in out
    assert "hidden list detail" not in out


def test_list_findings_all_scopes(tools):
    out = _call(tools["list_findings"], status="closed")
    assert "#8" in out
    assert "#7" not in out


def test_get_finding_full_record_and_history(tools):
    out = _call(tools["get_finding"], finding_id=7)
    for expected in ("summary:", "reproduction:", "impact:", "remediation:",
                     "evidence:", "history:", "curl -I", "informative"):
        assert expected in out


def test_start_scan_success_and_audit(tools, fake):
    fake.scans = []
    out = _call(tools["start_scan"], scope="*.example.com")
    assert "run-11" in out
    assert fake.activities[-1]["action"] == "start_scan"


def test_start_scan_refuses_when_running(tools, fake):
    out = _call(tools["start_scan"], scope="*.example.com")
    assert out.startswith("error:")
    assert "already running" in out
    assert not any(call[0] == "start_scan" for call in fake.calls)


def test_list_schedules_for_all_scopes(tools):
    out = _call(tools["list_schedules"])
    assert "cadence=daily" in out
    assert "enabled=yes" in out


def test_set_schedule_enabled_and_disabled(tools, fake):
    out = _call(tools["set_schedule"], scope="*.example.com",
                cadence="weekly", enabled=True)
    assert "cadence=weekly" in out
    assert ("set_schedule", 1, "weekly") in fake.calls
    assert fake.activities[-1]["action"] == "set_schedule"

    out = _call(tools["set_schedule"], scope="*.example.com", enabled=False)
    assert "cadence=off" in out
    assert ("set_schedule", 1, "off") in fake.calls


def test_set_schedule_rejects_bad_cadence(tools):
    out = _call(tools["set_schedule"], scope="*.example.com",
                cadence="every-minute")
    assert out.startswith("error:")


def test_recheck_finding(tools, fake):
    out = _call(tools["recheck_finding"], finding_id=7)
    assert "run-12" in out
    assert fake.activities[-1]["action"] == "recheck_finding"


def test_finding_lifecycle_mutations(tools, fake):
    resolve = _call(tools["resolve_finding"], finding_id=7, note="fixed")
    assert "closed" in resolve
    assert fake.activities[-1]["action"] == "resolve_finding"

    reopen = _call(tools["reopen_finding"], finding_id=8)
    assert "reopened" in reopen
    assert fake.activities[-1]["action"] == "reopen_finding"

    mark = _call(tools["mark_informative"], finding_id=7, note="known")
    assert "informative" in mark
    assert fake.activities[-1]["action"] == "mark_informative"

    unmark = _call(tools["unmark_informative"], finding_id=7)
    assert "returned to open" in unmark
    assert fake.activities[-1]["action"] == "unmark_informative"


ERROR_CASES = [
    ("list_scopes", {}, "list_scopes"),
    ("list_hosts", {"scope": "*.example.com"}, "list_hosts"),
    ("scan_status", {"scope": "*.example.com"}, "list_scans"),
    ("list_findings", {"scope": "*.example.com"}, "findings_for_scope"),
    ("get_finding", {"finding_id": 7}, "get_finding"),
    ("start_scan", {"scope": "*.example.com"}, "list_scans"),
    ("list_schedules", {"scope": "*.example.com"}, "get_schedule"),
    ("set_schedule", {"scope": "*.example.com", "cadence": "daily"},
     "get_schedule"),
    ("recheck_finding", {"finding_id": 7}, "verify_fix"),
    ("resolve_finding", {"finding_id": 7}, "set_finding_status"),
    ("reopen_finding", {"finding_id": 7}, "set_finding_status"),
    ("mark_informative", {"finding_id": 7}, "set_finding_status"),
    ("unmark_informative", {"finding_id": 7}, "set_finding_status"),
]


@pytest.mark.parametrize("tool_name,kwargs,failing_method", ERROR_CASES)
def test_every_tool_returns_readable_error(tools, fake, tool_name, kwargs,
                                           failing_method):
    fake.fail = {failing_method}
    out = _call(tools[tool_name], **kwargs)
    assert out.startswith("error:")
    assert "Traceback" not in out
    assert failing_method in out


def test_unknown_scope_is_readable_error(tools):
    out = _call(tools["list_hosts"], scope="nope.example.org")
    assert out.startswith("error:")
    assert "not found" in out


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_pro_client_uses_env_url_and_service_token(monkeypatch):
    from quarterdeck.platform_tools import ProClient

    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["actor"] = request.get_header("X-eyry-actor")
        return _Response(b'{"scopes": []}')

    monkeypatch.setenv("QD_PRO_API_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("QD_SERVICE_TOKEN", "secret-token")
    client = ProClient(opener=opener)
    assert client.list_scopes() == []
    assert seen["url"] == "http://127.0.0.1:9999/api/scopes/summary"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["actor"] == "self via quarterdeck"


def test_pro_client_defaults_to_loopback_8431(monkeypatch):
    from quarterdeck.platform_tools import ProClient

    monkeypatch.delenv("QD_PRO_API_URL", raising=False)
    monkeypatch.delenv("QD_SERVICE_TOKEN", raising=False)
    client = ProClient()
    assert client.base_url == "http://127.0.0.1:8431"
    assert client.token == ""


def test_pro_client_surfaces_http_detail(monkeypatch):
    import io
    import urllib.error

    from quarterdeck.platform_tools import ProAPIError, ProClient

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", {},
            io.BytesIO(b'{"detail": "a scan is already running"}'))

    client = ProClient(opener=opener)
    with pytest.raises(ProAPIError) as exc:
        client.start_scan(1)
    assert "409" in str(exc.value)
    assert "already running" in str(exc.value)
