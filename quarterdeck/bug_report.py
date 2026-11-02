"""Compose, redact, deduplicate, and file Quarterdeck bug reports.

The captain uses this module when a tool call, task, or operator-reported
feature failure needs to become a GitHub issue. Reports are redacted before
they leave the process, deduplicated by a stable signature embedded in the
issue body, and queued locally when no GitHub token is configured.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_registry import home_dir
from .locking import file_lock

TOKEN_ENV = "QUARTERDECK_GITHUB_TOKEN"
DEFAULT_REPO = "eyry-security/quarterdeck"
PRIVATE_REPO = "eyry-security/eyry-pro"
SIGNATURE_PREFIX = "quarterdeck-signature:"
SIGNATURE_MARKER = "<!-- " + SIGNATURE_PREFIX + "{signature} -->"
QUEUE_DIR = "bug_reports"
QUEUE_FILE = "queue.jsonl"
API_BASE = "https://api.github.com"
USER_AGENT = "quarterdeck-bug-report"

REPO_ROUTES = {
    "quarterdeck": "eyry-security/quarterdeck",
    "captain": PRIVATE_REPO,
    "platform": PRIVATE_REPO,
    "eyry-pro": PRIVATE_REPO,
    "seed": "eyry-security/quarterdeck",
    "aplomado": "eyry-security/aplomado",
    "pinnace": "eyry-security/pinnace",
    "foretop": "eyry-security/foretop",
    "vedette": "eyry-security/vedette",
    "purser": "eyry-security/purser",
    "rutt": "eyry-security/rutt",
    "fathom": "eyry-security/fathom",
    "magpie": "eyry-security/magpie",
    "credmeter": "eyry-security/credmeter",
    "eyry-website": "eyry-security/eyry-website",
}
SURFACE_REPOS = REPO_ROUTES
REPO_ROUTING = REPO_ROUTES

REDACTED_TOKEN = "[REDACTED_TOKEN]"
REDACTED_AUTH = "[REDACTED_AUTH]"
REDACTED_PASSWORD = "[REDACTED_PASSWORD]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_NAME = "[REDACTED_NAME]"
REDACTED_IP = "[REDACTED_IP]"
REDACTED_PRIVATE_KEY = "[REDACTED_PRIVATE_KEY]"

_SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASS|PWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"CREDENTIAL|AUTH)",
    re.IGNORECASE,
)
_NAME_ENV_KEYS = (
    "QD_OPERATOR_NAME",
    "OPERATOR_NAME",
    "FULL_NAME",
    "DISPLAY_NAME",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_NAME",
)
_USERNAME_ENV_KEYS = ("USER", "LOGNAME", "USERNAME")
_GENERIC_USERNAMES = {
    "root", "admin", "user", "test", "runner", "ubuntu", "nobody",
    "daemon", "service", "operator", "ec2-user", "vagrant",
}

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTH_HEADER_RE = re.compile(
    r"(?im)\b(authorization|proxy-authorization|x-api-key|x-auth-token)"
    r"\s*:\s*[^\r\n]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+")
_BASIC_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+")
_PASSWORD_RE = re.compile(
    r"(?i)(?<![\w.-])([A-Za-z0-9_.-]*(?:password|passwd|passphrase|pwd|pass))"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![\w.-])(?!(?:authorization|proxy-authorization|x-api-key|"
    r"x-auth-token)\b)([A-Za-z0-9_.-]*(?:token|secret|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|credential)"
    r"[A-Za-z0-9_.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_TOKEN_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
)
_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_RE = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"
)
_ISO_TIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_ID_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
_UNIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:[A-Za-z0-9._~+-]+/)*[A-Za-z0-9._~+-]+"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z]:\\(?:[^\\\s:'\"()\[\],;]+\\?)*"
)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_SPACE_RE = re.compile(r"\s+")


def _redact_ips(text: str) -> str:
    def replace(match: re.Match) -> str:
        candidate = match.group(0)
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return candidate
        return REDACTED_IP

    text = _IPV4_RE.sub(replace, text)
    return _IPV6_RE.sub(replace, text)


def _name_variants(name: str) -> set[str]:
    cleaned = _SPACE_RE.sub(" ", str(name or "")).strip()
    if len(cleaned) < 3:
        return set()
    if cleaned.lower() in _GENERIC_USERNAMES:
        return set()
    variants = {cleaned}
    parts = cleaned.split()
    if len(parts) > 1:
        for part in (parts[0], parts[-1]):
            if len(part) >= 3 and part.lower() not in _GENERIC_USERNAMES:
                variants.add(part)
    return variants


@dataclass
class Redactor:
    """Strict, repeatable redaction for bug reports and their logs."""

    secrets: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    redact_ips: bool = True

    def __post_init__(self) -> None:
        self.secrets = tuple(sorted(
            {str(item) for item in self.secrets if len(str(item)) >= 4},
            key=len, reverse=True,
        ))
        variants: set[str] = set()
        for name in self.names:
            variants.update(_name_variants(name))
        self._name_res = tuple(
            re.compile(rf"(?i)(?<!\w){re.escape(name)}(?!\w)")
            for name in sorted(variants, key=len, reverse=True)
        )

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None,
                         *, names: tuple[str, ...] | list[str] | None = None,
                         extra_secrets: tuple[str, ...] | list[str] | None = None,
                         redact_ips: bool = True) -> "Redactor":
        values = dict(os.environ if env is None else env)
        secrets = list(extra_secrets or ())
        for key, value in values.items():
            if _SECRET_ENV_RE.search(key) and len(value) >= 6:
                secrets.append(value)
        collected = list(names or ())
        for key in _NAME_ENV_KEYS:
            value = values.get(key, "").strip()
            if value:
                collected.append(value)
        for key in _USERNAME_ENV_KEYS:
            value = values.get(key, "").strip()
            if value and value.lower() not in _GENERIC_USERNAMES:
                collected.append(value)
        try:
            home_name = Path.home().name
            if home_name and home_name.lower() not in _GENERIC_USERNAMES:
                collected.append(home_name)
        except (RuntimeError, OSError):
            pass
        return cls(secrets=tuple(secrets), names=tuple(collected),
                   redact_ips=redact_ips)

    def redact(self, value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        for secret in self.secrets:
            if secret and secret in text:
                text = text.replace(secret, REDACTED_TOKEN)
        text = _PEM_RE.sub(REDACTED_PRIVATE_KEY, text)
        text = _PASSWORD_RE.sub(
            lambda match: f"{match.group(1)}=[REDACTED_PASSWORD]", text)
        text = _KEY_VALUE_RE.sub(
            lambda match: f"{match.group(1)}=[REDACTED_TOKEN]", text)
        text = _AUTH_HEADER_RE.sub(r"\1: " + REDACTED_AUTH, text)
        text = _BEARER_RE.sub("Bearer " + REDACTED_TOKEN, text)
        text = _BASIC_RE.sub("Basic " + REDACTED_AUTH, text)
        for pattern in _TOKEN_RES:
            text = pattern.sub(REDACTED_TOKEN, text)
        text = _EMAIL_RE.sub(REDACTED_EMAIL, text)
        for pattern in self._name_res:
            text = pattern.sub(REDACTED_NAME, text)
        if self.redact_ips:
            text = _redact_ips(text)
        return text


def redact(value: Any, *, names: tuple[str, ...] | list[str] | None = None,
           extra_secrets: tuple[str, ...] | list[str] | None = None,
           redact_ips: bool = True) -> str:
    """Redact one value using secrets/names from the current environment."""
    return Redactor.from_environment(
        names=names, extra_secrets=extra_secrets, redact_ips=redact_ips,
    ).redact(value)


redact_text = redact


def repo_for_surface(surface: str, routes: Mapping[str, str] | None = None) -> str:
    mapping = dict(routes or REPO_ROUTES)
    normalized = re.sub(r"[^a-z0-9]+", "-", str(surface or "").strip().lower())
    normalized = normalized.strip("-")
    if not normalized:
        return DEFAULT_REPO
    if normalized in mapping:
        return mapping[normalized]
    for key in sorted(mapping, key=len, reverse=True):
        if normalized.startswith(key + "-"):
            return mapping[key]
    return DEFAULT_REPO


@dataclass
class BugReport(Mapping[str, Any]):
    source_surface: str
    repo: str
    error_type: str
    error_message: str
    tool_name: str = ""
    task_name: str = ""
    log_lines: list[str] = field(default_factory=list)
    quarterdeck_version: str = ""
    timestamp: str = ""
    title: str = ""
    summary: str = ""
    signature: str = ""

    def __getitem__(self, key: str) -> Any:
        if key not in self._fields():
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self._fields())

    def __len__(self) -> int:
        return len(self._fields())

    @staticmethod
    def _fields() -> tuple[str, ...]:
        return (
            "source_surface", "repo", "error_type", "error_message",
            "tool_name", "task_name", "log_lines", "quarterdeck_version",
            "timestamp", "title", "summary", "signature",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def body(self) -> str:
        logs = "\n".join(self.log_lines) if self.log_lines else "(none captured)"
        details = [f"- Source surface: `{self.source_surface}`"]
        if self.tool_name:
            details.append(f"- Tool: `{self.tool_name}`")
        if self.task_name:
            details.append(f"- Task: `{self.task_name}`")
        details.append(f"- Error type: `{self.error_type}`")
        return "\n".join([
            SIGNATURE_MARKER.format(signature=self.signature),
            "",
            "## Summary",
            self.summary or "(no summary)",
            "",
            "## Failure",
            *details,
            "",
            "## Error message",
            "```",
            self.error_message or "(no error message)",
            "```",
            "",
            "## Recent logs",
            "```",
            logs,
            "```",
            "",
            "## Environment",
            f"- Quarterdeck version: {self.quarterdeck_version or 'unknown'}",
            f"- Timestamp: {self.timestamp}",
            "",
            "<!-- filed by Quarterdeck's report_bug tool -->",
        ])


def _context_value(context: Any, keys: tuple[str, ...], default: Any = "") -> Any:
    if isinstance(context, Mapping):
        for key in keys:
            if key in context and context[key] is not None:
                return context[key]
        return default
    for key in keys:
        value = getattr(context, key, None)
        if value is not None:
            return value
    return default


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        value = time.time()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
            "+00:00", "Z")
    return str(value)


def _one_line(value: str, limit: int = 200) -> str:
    return _SPACE_RE.sub(" ", value).strip()[:limit]


def compose_report(context: Any, *, redactor: Redactor | None = None) -> BugReport:
    """Build a redacted report from a failure context."""
    source_surface = str(_context_value(
        context, ("source_surface", "source", "surface"), "quarterdeck",
    )).strip() or "quarterdeck"
    raw_repo = str(_context_value(context, ("repo", "repository"), "")).strip()
    error_type = str(_context_value(
        context, ("error_type", "type", "error_class"), "UnknownError",
    )).strip() or "UnknownError"
    error_message = str(_context_value(
        context, ("error_message", "message", "error"), "",
    ))
    tool_name = str(_context_value(context, ("tool_name", "tool"), "")).strip()
    task_name = str(_context_value(context, ("task_name", "task"), "")).strip()
    summary = str(_context_value(
        context, ("summary", "complaint", "description"), "",
    )).strip()
    version = str(_context_value(
        context, ("quarterdeck_version", "version"), "",
    )).strip()
    timestamp = _format_timestamp(_context_value(
        context, ("timestamp", "ts"), None,
    ))
    logs = _context_value(
        context, ("log_lines", "recent_logs", "logs"), (),
    )
    if isinstance(logs, str):
        log_lines = logs.splitlines()
    else:
        try:
            log_lines = [str(line) for line in logs]
        except TypeError:
            log_lines = [str(logs)]

    redactor = redactor or Redactor.from_environment()
    if not version:
        try:
            from . import __version__ as qd_version
            version = qd_version
        except Exception:
            version = "unknown"

    redacted_message = redactor.redact(error_message)
    redacted_type = redactor.redact(error_type)
    redacted_surface = redactor.redact(source_surface)
    redacted_summary = _one_line(
        redactor.redact(summary) if summary else redacted_message,
    ) or "unspecified failure"
    title = _one_line(
        f"[{redacted_surface}] {redacted_type}: {redacted_summary}", 140,
    )
    report = BugReport(
        source_surface=redacted_surface,
        repo=raw_repo or repo_for_surface(source_surface),
        error_type=redacted_type,
        error_message=redacted_message,
        tool_name=redactor.redact(tool_name),
        task_name=redactor.redact(task_name),
        log_lines=[redactor.redact(line) for line in log_lines][-25:],
        quarterdeck_version=redactor.redact(version),
        timestamp=timestamp,
        title=title,
        summary=redacted_summary,
    )
    report.signature = signature(report)
    return report


def _normalize_message(message: str) -> str:
    text = str(message or "")
    text = _UUID_RE.sub("<id>", text)
    text = _ISO_TIME_RE.sub("<timestamp>", text)
    text = _DATE_RE.sub("<date>", text)
    text = _UNIX_PATH_RE.sub("<path>", text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _HEX_ID_RE.sub("<id>", text)
    text = _NUMBER_RE.sub("<num>", text)
    return _SPACE_RE.sub(" ", text).strip().lower()


def signature(report: Any) -> str:
    """Stable dedupe signature: repo + error class + normalized message."""
    if isinstance(report, Mapping):
        repo = str(report.get("repo", "") or DEFAULT_REPO)
        error_type = str(report.get("error_type", "") or "")
        message = str(report.get("error_message", "") or "")
    else:
        repo = str(getattr(report, "repo", "") or DEFAULT_REPO)
        error_type = str(getattr(report, "error_type", "") or "")
        message = str(getattr(report, "error_message", "") or "")
    material = "\n".join([
        repo.strip().lower(),
        error_type.strip().lower(),
        _normalize_message(message),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class TransportResponse:
    status: int
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class UrllibTransport:
    """Default GitHub transport; the only component that touches the network."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def request(self, method: str, url: str,
                headers: Mapping[str, str] | None = None,
                body: Any = None) -> TransportResponse:
        data = None
        request_headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
        payload = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = raw.decode("utf-8", "replace")
        return TransportResponse(status=status, data=payload,
                                 headers=response_headers)


class BugReportError(Exception):
    """A readable failure filing or searching for a bug report."""


@dataclass
class FilingResult:
    status: str
    message: str
    repo: str
    issue_number: int | None = None
    issue_url: str = ""
    queued_path: str = ""
    duplicate: bool = False
    report: BugReport | None = None

    @property
    def filed(self) -> bool:
        return self.status in ("created", "commented")


def _response_parts(response: Any) -> tuple[int, Any]:
    if isinstance(response, TransportResponse):
        return response.status, response.data
    if isinstance(response, tuple) and len(response) == 2:
        return int(response[0]), response[1]
    status = getattr(response, "status", 200)
    data = getattr(response, "data", response)
    return int(status), data


def _state_dir(home: Path | str | None) -> Path:
    if home:
        return Path(home)
    raw = os.environ.get("QD_STATE_DIR", "").strip()
    return Path(raw) if raw else home_dir()


def _queue_path(home: Path | str | None) -> Path:
    return _state_dir(home) / QUEUE_DIR / QUEUE_FILE


def queue_report(report: BugReport, home: Path | str | None,
                 queued_at: str | None = None) -> Path:
    path = _queue_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "queued_at": queued_at or _format_timestamp(None),
        "report": report.to_dict(),
    }
    with file_lock(path.with_suffix(".lock")):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return path


class GitHubIssuesClient:
    """Search, create, and comment on GitHub issues behind a transport."""

    def __init__(self, token: str | None = None, *, transport: Any = None,
                 repo_map: Mapping[str, str] | None = None,
                 home: Path | str | None = None,
                 api_base: str = API_BASE,
                 token_env: str = TOKEN_ENV):
        self.token = (os.environ.get(token_env, "").strip()
                      if token is None else str(token).strip())
        self.transport = transport or UrllibTransport()
        self.repo_map = dict(repo_map or REPO_ROUTES)
        self.home = home
        self.api_base = api_base.rstrip("/")

    def repo_for(self, report: BugReport) -> str:
        return report.repo or repo_for_surface(
            report.source_surface, self.repo_map)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, url: str, body: Any = None) -> Any:
        try:
            response = self.transport.request(
                method, url, headers=self._headers(), body=body)
        except Exception as exc:
            raise BugReportError(f"GitHub request failed: {exc}") from exc
        status, data = _response_parts(response)
        if status < 200 or status >= 300:
            detail = ""
            if isinstance(data, Mapping):
                detail = str(data.get("message", ""))
            elif data:
                detail = str(data)[:300]
            raise BugReportError(
                f"GitHub returned {status}: {detail or 'request failed'}")
        return data

    def search_open_duplicate(self, report: BugReport) -> dict | None:
        repo = self.repo_for(report)
        sig = report.signature or signature(report)
        query = (
            f'repo:{repo} is:issue is:open '
            f'"{SIGNATURE_PREFIX}{sig}" in:body'
        )
        url = (f"{self.api_base}/search/issues?"
               f"{urllib.parse.urlencode({'q': query, 'per_page': 20})}")
        data = self._request("GET", url)
        marker = SIGNATURE_MARKER.format(signature=sig)
        items = data.get("items", []) if isinstance(data, Mapping) else []
        for item in items:
            if marker in str(item.get("body", "")):
                return item
        return None

    def create_issue(self, report: BugReport) -> dict:
        repo = self.repo_for(report)
        url = f"{self.api_base}/repos/{repo}/issues"
        data = self._request("POST", url, {
            "title": report.title,
            "body": report.body(),
        })
        return data if isinstance(data, Mapping) else {}

    def comment_seen_again(self, report: BugReport, issue_number: int) -> dict:
        repo = self.repo_for(report)
        url = (
            f"{self.api_base}/repos/{repo}/issues/{issue_number}/comments"
        )
        detail = f"`{report.source_surface}`"
        if report.tool_name:
            detail += f" tool `{report.tool_name}`"
        if report.task_name:
            detail += f" task `{report.task_name}`"
        body = "\n".join([
            f"Seen again at {report.timestamp} from {detail}.",
            "",
            report.summary,
            "",
            "<!-- quarterdeck-seen-again -->",
        ])
        data = self._request("POST", url, {"body": body})
        return data if isinstance(data, Mapping) else {}

    def file(self, report: BugReport) -> FilingResult:
        repo = self.repo_for(report)
        report.repo = repo
        if not report.signature:
            report.signature = signature(report)
        if not self.token:
            path = queue_report(report, self.home)
            return FilingResult(
                status="queued",
                message=(
                    f"No {TOKEN_ENV} is set; bug report queued locally at "
                    f"{path} — not filed. Title: {report.title}"
                ),
                repo=repo,
                queued_path=str(path),
                report=report,
            )
        duplicate = self.search_open_duplicate(report)
        if duplicate:
            number = duplicate.get("number")
            try:
                issue_number = int(number)
            except (TypeError, ValueError):
                raise BugReportError(
                    "GitHub search returned a duplicate without an issue number")
            self.comment_seen_again(report, issue_number)
            issue_url = str(duplicate.get("html_url", ""))
            return FilingResult(
                status="commented",
                message=(
                    f"Duplicate found (#{issue_number}); added a 'seen again' "
                    f"comment instead of creating a new issue: {issue_url}"
                ),
                repo=repo,
                issue_number=issue_number,
                issue_url=issue_url,
                duplicate=True,
                report=report,
            )
        created = self.create_issue(report)
        number = created.get("number")
        issue_number = None
        try:
            if number is not None:
                issue_number = int(number)
        except (TypeError, ValueError):
            issue_number = None
        issue_url = str(created.get("html_url", ""))
        return FilingResult(
            status="created",
            message=(
                f"Filed bug report "
                f"{'#' + str(issue_number) if issue_number else ''} in {repo}: "
                f"{issue_url}"
            ),
            repo=repo,
            issue_number=issue_number,
            issue_url=issue_url,
            report=report,
        )


def format_preview(report: BugReport) -> str:
    return "\n".join([
        "Bug report preview (nothing filed):",
        f"Title: {report.title}",
        f"Summary: {report.summary}",
        f"Repository: {report.repo}",
        f"Signature: {report.signature}",
        'Ask the operator: "do you want me to submit this bug report?"',
    ])


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "y", "on", "confirmed",
    }


REPORT_BUG_DESCRIPTION = (
    "File a GitHub bug report for a Quarterdeck failure or an operator "
    "complaint. Flow: detect a failed tool/task or a feature the operator says "
    "did not work; call this with confirmed=false to compose a redacted "
    "preview; tell the operator the title and one-line summary and ask "
    '"do you want me to submit this bug report?"; call again with '
    "confirmed=true ONLY after an affirmative reply in the conversation. "
    "Never file without confirmation. Args: source_surface (quarterdeck, "
    "captain, platform, or a component such as aplomado/pinnace), error_type, "
    "error_message, confirmed (bool), tool_name, task_name, recent_logs, "
    "summary. With confirmed=false it returns the preview and files nothing."
)


def report_bug_tool(agent_name: str = "self", home: Path | str | None = None,
                    client: GitHubIssuesClient | None = None):
    from langchain_core.tools import StructuredTool

    def report_bug(source_surface: str = "quarterdeck",
                   error_type: str = "UnknownError",
                   error_message: str = "",
                   confirmed: bool = False,
                   tool_name: str = "",
                   task_name: str = "",
                   recent_logs: str | list[str] = "",
                   summary: str = "") -> str:
        report = compose_report({
            "source_surface": source_surface,
            "error_type": error_type,
            "error_message": error_message,
            "tool_name": tool_name,
            "task_name": task_name,
            "recent_logs": recent_logs,
            "summary": summary,
        })
        if not _as_bool(confirmed):
            return format_preview(report)
        reporter = client or GitHubIssuesClient(home=home)
        try:
            result = reporter.file(report)
        except Exception as exc:
            return f"error: could not file bug report: {exc}"
        return result.message

    return StructuredTool.from_function(
        func=report_bug,
        name="report_bug",
        description=REPORT_BUG_DESCRIPTION,
    )
