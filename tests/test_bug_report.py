"""Bug-report composition, redaction, dedupe, queueing, and tool flow."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from quarterdeck.bug_report import (
    DEFAULT_REPO,
    PRIVATE_REPO,
    SIGNATURE_MARKER,
    SIGNATURE_PREFIX,
    BugReportError,
    GitHubIssuesClient,
    Redactor,
    TransportResponse,
    compose_report,
    format_preview,
    redact,
    repo_for_surface,
    report_bug_tool,
    signature,
)


def _context(**overrides):
    value = {
        "source_surface": "quarterdeck",
        "error_type": "RuntimeError",
        "error_message": "task failed: connection reset",
        "tool_name": "quarterdeck_wake",
        "task_name": "watch-scope",
        "recent_logs": ["wake failed", "retrying"],
        "summary": "waking the agent failed",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "text,leaked,marker",
    [
        ("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
         "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[REDACTED_TOKEN]"),
        ("api_key=sk-abcdefghijklmnopqrst", "sk-abcdefghijklmnopqrst",
         "[REDACTED_TOKEN]"),
        ("password=Sup3rSecret!", "Sup3rSecret!", "[REDACTED_PASSWORD]"),
        ("DB_PASSWORD=hunter2", "hunter2", "[REDACTED_PASSWORD]"),
        ("Authorization: Bearer abcdef123456", "abcdef123456",
         "[REDACTED_AUTH]"),
        ("x-api-key: abcdef123456", "abcdef123456", "[REDACTED_AUTH]"),
        ("contact ops@example.test", "ops@example.test", "[REDACTED_EMAIL]"),
        ("host 203.0.113.42 failed", "203.0.113.42", "[REDACTED_IP]"),
        ("host 2001:db8::1 failed", "2001:db8::1", "[REDACTED_IP]"),
    ],
)
def test_redaction_classes(text, leaked, marker):
    out = redact(text)
    assert leaked not in out
    assert marker in out


def test_redaction_uses_environment_name_and_secret(monkeypatch):
    monkeypatch.setenv("QD_OPERATOR_NAME", "Example Operator")
    monkeypatch.setenv("QUARTERDECK_GITHUB_TOKEN", "plain-secret-value")
    out = redact("Example Operator sent plain-secret-value from the deck")
    assert "Example Operator" not in out
    assert "plain-secret-value" not in out
    assert "[REDACTED_NAME]" in out
    assert "[REDACTED_TOKEN]" in out


def test_redaction_accepts_explicit_names():
    redactor = Redactor(names=("Example Operator", "SampleUser"))
    out = redactor.redact("Example Operator told SampleUser about it")
    assert "Example Operator" not in out
    assert "SampleUser" not in out
    assert out.count("[REDACTED_NAME]") == 2


def test_compose_report_redacts_every_field_before_output():
    report = compose_report(_context(
        error_message=(
            "Authorization: Bearer abcdef123456 for ops@example.test at "
            "203.0.113.42 password=Sup3rSecret!"
        ),
        recent_logs=[
            "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "api_key=sk-abcdefghijklmnopqrst",
            "contact ops@example.test",
        ],
        summary="Example Operator saw 203.0.113.42 fail",
    ), redactor=Redactor(names=("Example Operator",)))
    blob = "\n".join([report.title, report.summary, report.body()])
    for leaked in (
        "abcdef123456", "ops@example.test", "203.0.113.42", "Sup3rSecret!",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-abcdefghijklmnopqrst", "Example Operator",
    ):
        assert leaked not in blob
    assert report.repo == DEFAULT_REPO
    assert report.quarterdeck_version
    assert report.timestamp
    assert SIGNATURE_PREFIX in report.body()


def test_signature_is_stable_across_volatile_parts():
    first = compose_report(_context(
        error_message=(
            "Scan 123 failed at 2026-10-04T12:00:00Z for "
            "/tmp/run-123/output.log (job 550e8400-e29b-41d4-a716-446655440000)"
        ),
        timestamp=1000,
    ))
    second = compose_report(_context(
        error_message=(
            "Scan 987 failed at 2026-10-05T09:30:00Z for "
            "/var/log/run-987/output.log (job 123e4567-e89b-12d3-a456-426614174000)"
        ),
        timestamp=2000,
    ))
    assert first.error_message != second.error_message
    assert signature(first) == signature(second)
    assert first.signature == second.signature


def test_signature_changes_with_error_class_or_repo():
    base = compose_report(_context(error_message="same failure 123"))
    other_type = compose_report(_context(
        error_type="TimeoutError", error_message="same failure 123",
    ))
    other_repo = compose_report(_context(
        source_surface="aplomado", error_message="same failure 123",
    ))
    assert signature(base) != signature(other_type)
    assert signature(base) != signature(other_repo)


def test_repo_routing_table():
    assert repo_for_surface("quarterdeck") == "eyry-security/quarterdeck"
    assert repo_for_surface("captain") == PRIVATE_REPO
    assert repo_for_surface("platform tool") == PRIVATE_REPO
    assert repo_for_surface("eyry-pro") == PRIVATE_REPO
    assert repo_for_surface("aplomado") == "eyry-security/aplomado"
    assert repo_for_surface("pinnace") == "eyry-security/pinnace"
    assert repo_for_surface("unknown-component") == DEFAULT_REPO


class FakeTransport:
    def __init__(self, duplicate: dict | None = None):
        self.duplicate = duplicate
        self.requests: list[dict] = []
        self.created_number = 101

    def request(self, method, url, headers=None, body=None):
        self.requests.append({
            "method": method, "url": url, "headers": headers, "body": body,
        })
        if "/search/issues" in url:
            items = [self.duplicate] if self.duplicate else []
            return TransportResponse(200, {
                "total_count": len(items), "items": items,
            })
        if method == "POST" and url.endswith("/comments"):
            return TransportResponse(201, {"id": 9})
        if method == "POST" and url.endswith("/issues"):
            return TransportResponse(201, {
                "number": self.created_number,
                "html_url": (
                    f"https://github.com/{DEFAULT_REPO}/issues/"
                    f"{self.created_number}"
                ),
            })
        return TransportResponse(404, {"message": "not found"})


def test_full_mocked_transport_creates_issue_with_signature_marker():
    transport = FakeTransport()
    client = GitHubIssuesClient(token="test-token", transport=transport)
    report = compose_report(_context())

    result = client.file(report)

    assert result.status == "created"
    assert result.filed
    assert result.issue_number == 101
    assert len(transport.requests) == 2
    search, create = transport.requests
    assert search["method"] == "GET"
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(search["url"]).query)["q"][0]
    assert f"repo:{DEFAULT_REPO}" in query
    assert f"{SIGNATURE_PREFIX}{report.signature}" in query
    assert create["method"] == "POST"
    assert create["url"].endswith(f"/repos/{DEFAULT_REPO}/issues")
    assert create["body"]["title"] == report.title
    assert SIGNATURE_PREFIX + report.signature in create["body"]["body"]
    assert transport.requests[0]["headers"]["Authorization"] == (
        "Bearer test-token")


def test_duplicate_found_comments_instead_of_creating():
    report = compose_report(_context())
    duplicate = {
        "number": 55,
        "html_url": f"https://github.com/{DEFAULT_REPO}/issues/55",
        "body": SIGNATURE_MARKER.format(signature=report.signature),
    }
    transport = FakeTransport(duplicate=duplicate)
    client = GitHubIssuesClient(token="test-token", transport=transport)

    result = client.file(report)

    assert result.status == "commented"
    assert result.duplicate
    assert result.issue_number == 55
    assert len(transport.requests) == 2
    comment = transport.requests[1]
    assert comment["method"] == "POST"
    assert comment["url"].endswith("/issues/55/comments")
    assert "Seen again" in comment["body"]["body"]
    assert report.timestamp in comment["body"]["body"]
    assert not any(
        request["method"] == "POST"
        and request["url"].endswith("/issues")
        and not request["url"].endswith("/comments")
        for request in transport.requests
    )


def test_no_token_queues_locally_and_says_not_filed(tmp_path):
    transport = FakeTransport()
    client = GitHubIssuesClient(token="", transport=transport, home=tmp_path)
    report = compose_report(_context())

    result = client.file(report)

    assert result.status == "queued"
    assert not result.filed
    assert "queued" in result.message.lower()
    assert "not filed" in result.message.lower()
    assert transport.requests == []
    queue = tmp_path / "bug_reports" / "queue.jsonl"
    assert queue.exists()
    record = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
    assert record["report"]["signature"] == report.signature
    assert record["report"]["title"] == report.title


def test_unconfirmed_tool_call_never_files_or_queues(tmp_path):
    transport = FakeTransport()
    client = GitHubIssuesClient(token="test-token", transport=transport,
                                home=tmp_path)
    tool = report_bug_tool("self", home=tmp_path, client=client)

    out = tool.invoke(_context(confirmed=False))

    assert "Bug report preview" in out
    assert "Title:" in out
    assert "Summary:" in out
    assert "nothing filed" in out.lower()
    assert transport.requests == []
    assert not (tmp_path / "bug_reports").exists()


def test_confirmed_tool_call_files_through_mocked_transport(tmp_path):
    transport = FakeTransport()
    client = GitHubIssuesClient(token="test-token", transport=transport,
                                home=tmp_path)
    tool = report_bug_tool("self", home=tmp_path, client=client)

    out = tool.invoke(_context(confirmed=True))

    assert "Filed bug report #101" in out
    assert len(transport.requests) == 2


def test_tool_queue_path_plainly_reports_queued_not_filed(tmp_path):
    transport = FakeTransport()
    client = GitHubIssuesClient(token="", transport=transport, home=tmp_path)
    tool = report_bug_tool("self", home=tmp_path, client=client)

    out = tool.invoke(_context(confirmed=True))

    assert "queued" in out.lower()
    assert "not filed" in out.lower()
    assert transport.requests == []
    assert (tmp_path / "bug_reports" / "queue.jsonl").exists()


def test_tool_description_teaches_confirmation_flow():
    tool = report_bug_tool("self")
    description = tool.description.lower()
    assert "do you want me to submit this bug report?" in description
    assert "confirmed=false" in description
    assert "confirmed=true" in description
    assert "never file without confirmation" in description


def test_preview_contains_title_and_summary_only():
    report = compose_report(_context())
    preview = format_preview(report)
    assert report.title in preview
    assert report.summary in preview
    assert "do you want me to submit this bug report?" in preview


def test_github_error_is_readable():
    class FailingTransport:
        def request(self, method, url, headers=None, body=None):
            return TransportResponse(403, {"message": "forbidden"})

    client = GitHubIssuesClient(token="test-token",
                                transport=FailingTransport())
    with pytest.raises(BugReportError) as exc:
        client.file(compose_report(_context()))
    assert "403" in str(exc.value)
    assert "forbidden" in str(exc.value)
