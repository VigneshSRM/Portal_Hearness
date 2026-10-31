"""Configuration loading.

Settings come from (lowest to highest precedence):

1. Built-in defaults (the dataclasses below)
2. ``config/settings.yaml`` (or the file given via ``--config`` / ``REDMINE_AGENT_CONFIG``)
3. Environment variables (also read from a local ``.env`` file when python-dotenv is installed)

String values in the YAML file may reference environment variables with
``${VAR}`` or ``${VAR:-default}``, so secrets never need to live in YAML.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import ConfigError

DEFAULT_CONFIG_CANDIDATES = ("config/settings.yaml", "config/settings.yml", "settings.yaml")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --------------------------------------------------------------------------- sections


@dataclass
class RedmineConfig:
    url: str = "http://localhost:3000"
    public_url: str = ""  # browser-facing URL for links, if different from `url`
    api_key: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    ca_bundle: str = ""
    timeout: float = 30.0
    page_size: int = 100
    max_retries: int = 3
    max_items: int = 5000
    impersonate: str = ""
    read_only: bool = False
    project_ids: list[str] = field(default_factory=list)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key or (self.username and self.password))


@dataclass
class SLARule:
    """First matching rule wins. Empty match fields match everything."""

    name: str = ""
    priority: str = ""
    tracker: str = ""
    project: str = ""
    response_hours: float | None = None
    resolution_hours: float | None = None

    def matches(self, priority: str, tracker: str, project: str) -> bool:
        def ok(expected: str, actual: str) -> bool:
            if not expected:
                return True
            options = [o.strip().lower() for o in expected.split("|")]
            return (actual or "").strip().lower() in options

        return ok(self.priority, priority) and ok(self.tracker, tracker) and ok(self.project, project)

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        parts = [p for p in (self.priority, self.tracker, self.project) if p]
        return " / ".join(parts) or "default"


@dataclass
class BusinessHoursConfig:
    enabled: bool = True
    start: str = "09:00"
    end: str = "18:00"
    workdays: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    holidays: list[str] = field(default_factory=list)


@dataclass
class SLAConfig:
    rules: list[SLARule] = field(
        default_factory=lambda: [
            SLARule(priority="Immediate", response_hours=1, resolution_hours=4),
            SLARule(priority="Urgent", response_hours=2, resolution_hours=8),
            SLARule(priority="High", response_hours=4, resolution_hours=24),
            SLARule(priority="Normal", response_hours=8, resolution_hours=72),
            SLARule(priority="Low", response_hours=24, resolution_hours=120),
        ]
    )
    default_response_hours: float | None = 8
    default_resolution_hours: float | None = 72
    at_risk_ratio: float = 0.8
    business_hours: BusinessHoursConfig = field(default_factory=BusinessHoursConfig)
    track_first_response: bool = True
    first_response_max_issues: int = 400
    response_ignore_private_notes: bool = True


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    security: str = "starttls"  # starttls | ssl | none
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject_prefix: str = "[Redmine]"
    send_time: str = "09:00"
    send_days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    lookback_hours: int = 24
    sla_window_days: int = 30
    attach_csv: bool = True
    max_rows: int = 15
    dashboard_url: str = ""
    timeout: float = 30.0


@dataclass
class ClosureConfig:
    scope: str = "me"  # me | all
    candidate_statuses: list[str] = field(default_factory=lambda: ["Resolved"])
    idle_days: int = 3
    require_done_ratio: int | None = None
    close_status: str = "Closed"
    default_template: str = "standard-resolution"
    template_rules: list[dict[str, str]] = field(default_factory=list)
    templates_file: str = "data/closure_templates.yaml"
    seed_templates_file: str = "config/closure_templates.yaml"
    auto_propose_on_schedule: bool = True
    verify_after_execute: bool = True


@dataclass
class TeamConfig:
    members: list[str] = field(default_factory=list)  # names, logins or ids; empty = everyone
    capacity_hours_per_day: float = 8.0


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    default_days: int = 30
    cache_ttl_seconds: int = 120
    username: str = ""
    password: str = ""
    public_url: str = ""


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.1
    max_steps: int = 8
    timeout: float = 90.0
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and (self.api_key or "localhost" in self.base_url
                                                       or "127.0.0.1" in self.base_url))


@dataclass
class Settings:
    timezone: str = "Asia/Kolkata"
    data_dir: str = "data"
    redmine: RedmineConfig = field(default_factory=RedmineConfig)
    sla: SLAConfig = field(default_factory=SLAConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    closure: ClosureConfig = field(default_factory=ClosureConfig)
    team: TeamConfig = field(default_factory=TeamConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    reports: dict[str, Any] = field(default_factory=dict)
    config_path: str = ""
    base_dir: str = ""

    # -- helpers -------------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return get_zone(self.timezone)

    def path(self, value: str | os.PathLike[str]) -> Path:
        """Resolve a (possibly relative) path against the harness base directory."""
        p = Path(value).expanduser()
        if p.is_absolute():
            return p
        return Path(self.base_dir or os.getcwd()) / p

    @property
    def data_path(self) -> Path:
        path = self.path(self.data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def report_presets(self) -> dict[str, dict[str, Any]]:
        presets = self.reports.get("presets") if isinstance(self.reports, dict) else None
        return dict(presets or {})


# --------------------------------------------------------------------------- loading


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"Unknown timezone '{name}'. Use an IANA name such as 'Asia/Kolkata'. "
            "On Windows install the 'tzdata' package."
        ) from exc


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            return os.environ.get(var, default if default is not None else "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, "")]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _coerce(value: Any, annotation: Any, default: Any) -> Any:
    """Best-effort coercion of YAML/env values to the dataclass field type."""
    ann = str(annotation)
    if value is None:
        return None if "None" in ann else default
    if ann.startswith("bool"):
        return _to_bool(value)
    if ann.startswith("int"):
        if value == "" and "None" in ann:
            return None
        return int(value)
    if ann.startswith("float"):
        if value == "" and "None" in ann:
            return None
        return float(value)
    if ann.startswith("list[str]"):
        return [str(v) for v in _to_list(value)]
    if ann.startswith("str"):
        return str(value)
    return value


def _build(cls: type, data: dict[str, Any] | None) -> Any:
    data = dict(data or {})
    obj = cls()
    known = {f.name: f for f in fields(cls)}
    for key, raw in data.items():
        if key not in known:
            continue
        f = known[key]
        current = getattr(obj, key)
        if is_dataclass(current):
            setattr(obj, key, _build(type(current), raw if isinstance(raw, dict) else {}))
        elif key == "rules" and cls is SLAConfig:
            setattr(obj, key, [_build(SLARule, r) for r in (raw or []) if isinstance(r, dict)])
        else:
            setattr(obj, key, _coerce(raw, f.type, current))
    return obj


_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "REDMINE_URL": ("redmine", "url"),
    "REDMINE_PUBLIC_URL": ("redmine", "public_url"),
    "REDMINE_API_KEY": ("redmine", "api_key"),
    "REDMINE_USERNAME": ("redmine", "username"),
    "REDMINE_PASSWORD": ("redmine", "password"),
    "REDMINE_VERIFY_SSL": ("redmine", "verify_ssl"),
    "REDMINE_CA_BUNDLE": ("redmine", "ca_bundle"),
    "REDMINE_READ_ONLY": ("redmine", "read_only"),
    "REDMINE_IMPERSONATE": ("redmine", "impersonate"),
    "REDMINE_PROJECTS": ("redmine", "project_ids"),
    "HARNESS_TIMEZONE": ("timezone",),
    "HARNESS_DATA_DIR": ("data_dir",),
    "EMAIL_ENABLED": ("email", "enabled"),
    "SMTP_HOST": ("email", "smtp_host"),
    "SMTP_PORT": ("email", "smtp_port"),
    "SMTP_SECURITY": ("email", "security"),
    "SMTP_USERNAME": ("email", "username"),
    "SMTP_PASSWORD": ("email", "password"),
    "SMTP_SENDER": ("email", "sender"),
    "DAILY_RECIPIENTS": ("email", "recipients"),
    "DAILY_SEND_TIME": ("email", "send_time"),
    "DASHBOARD_HOST": ("dashboard", "host"),
    "DASHBOARD_PORT": ("dashboard", "port"),
    "DASHBOARD_USERNAME": ("dashboard", "username"),
    "DASHBOARD_PASSWORD": ("dashboard", "password"),
    "DASHBOARD_PUBLIC_URL": ("dashboard", "public_url"),
    "LLM_BASE_URL": ("llm", "base_url"),
    "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"),
}


def _apply_env_overrides(settings: Settings) -> None:
    for env_name, path in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        target: Any = settings
        for part in path[:-1]:
            target = getattr(target, part)
        name = path[-1]
        f = {fl.name: fl for fl in fields(target)}[name]
        setattr(target, name, _coerce(raw, f.type, getattr(target, name)))


def _load_dotenv(base_dir: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return
    env_file = base_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def find_config_file(explicit: str | os.PathLike[str] | None, base_dir: Path) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return path
    env_path = os.environ.get("REDMINE_AGENT_CONFIG")
    if env_path:
        return find_config_file(env_path, base_dir)
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        path = base_dir / candidate
        if path.exists():
            return path
    return None


def load_settings(
    config_path: str | os.PathLike[str] | None = None,
    *,
    base_dir: str | os.PathLike[str] | None = None,
    overrides: dict[str, Any] | None = None,
    use_env: bool = True,
) -> Settings:
    """Load settings from YAML + environment.

    ``overrides`` is a nested dict applied after the YAML file (handy in tests).
    """
    root = Path(base_dir or os.environ.get("REDMINE_AGENT_HOME") or os.getcwd()).resolve()
    if use_env:
        _load_dotenv(root)
    path = find_config_file(config_path, root)
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a mapping at the top level")
    raw = _interpolate(raw)
    if overrides:
        raw = _deep_merge(raw, overrides)
    settings: Settings = _build(Settings, raw)
    if use_env:
        _apply_env_overrides(settings)
    settings.config_path = str(path) if path else ""
    settings.base_dir = str(root)
    validate_settings(settings)
    return settings


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def validate_settings(settings: Settings) -> None:
    get_zone(settings.timezone)
    url = settings.redmine.url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"redmine.url must start with http:// or https:// (got '{url}')")
    settings.redmine.url = url.rstrip("/")
    public = settings.redmine.public_url.strip()
    if public and not public.lower().startswith(("http://", "https://")):
        raise ConfigError(f"redmine.public_url must start with http:// or https:// (got '{public}')")
    settings.redmine.public_url = public.rstrip("/")
    for day in settings.sla.business_hours.workdays + settings.email.send_days:
        if day.lower()[:3] not in WEEKDAYS:
            raise ConfigError(f"Unknown weekday '{day}'. Use mon, tue, wed, thu, fri, sat, sun.")
    for label, value in (("sla.business_hours.start", settings.sla.business_hours.start),
                         ("sla.business_hours.end", settings.sla.business_hours.end),
                         ("email.send_time", settings.email.send_time)):
        if not re.fullmatch(r"\d{1,2}:\d{2}", value or ""):
            raise ConfigError(f"{label} must be HH:MM (got '{value}')")
    if settings.email.security not in {"starttls", "ssl", "none"}:
        raise ConfigError("email.security must be one of: starttls, ssl, none")
    if settings.closure.scope not in {"me", "all"}:
        raise ConfigError("closure.scope must be 'me' or 'all'")
    if not 0 < settings.sla.at_risk_ratio <= 1:
        raise ConfigError("sla.at_risk_ratio must be between 0 and 1")
    settings.redmine.page_size = max(1, min(int(settings.redmine.page_size), 100))


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]
