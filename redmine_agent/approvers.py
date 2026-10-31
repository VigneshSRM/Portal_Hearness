"""Registered approvers protected by a personal PIN.

Optional, and strongly recommended when AI agents operate the harness from a terminal
(Claude Code, Codex, Gemini CLI, Copilot CLI...). Once at least one approver is
registered, **every approval** - CLI, dashboard or agent chat - must come from a
registered approver *and* include that person's PIN.

Why: modern terminal agents can drive interactive programs through pseudo-terminals,
so "is someone at the keyboard?" checks are not enough on their own. A PIN that only
the person knows cannot be produced by the agent. (It is not a defence against someone
deliberately tampering with the harness's files - keep agents' changes reviewable.)

PINs are stored as salted PBKDF2-SHA256 hashes in ``<data_dir>/approvers.json``. They are
never stored in plain text, never logged and never passed to an LLM. Repeated wrong PINs
lock the approver for a while.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import HarnessError, PermissionDenied
from .guard import is_human_actor

ITERATIONS = 240_000
MIN_PIN_LENGTH = 6
MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _hash(pin: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), iterations).hex()


class ApproverRegistry:
    def __init__(self, path: str | Path, audit: Callable[..., None] | None = None) -> None:
        self.path = Path(path)
        self.audit = audit
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"approvers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise HarnessError(f"{self.path} is unreadable ({exc}). Fix or delete it.") from exc
        data.setdefault("approvers", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".approvers-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - e.g. Windows
            pass

    def _log(self, actor: str, action: str, outcome: str = "ok", **details: Any) -> None:
        if self.audit:
            self.audit(actor=actor, action=action, target=f"approver:{actor}", outcome=outcome, details=details)

    # ------------------------------------------------------------------ queries
    @property
    def configured(self) -> bool:
        return bool(self._load()["approvers"])

    def list(self) -> list[dict[str, Any]]:
        out = []
        for rec in self._load()["approvers"].values():
            locked_until = self._locked_until(rec)
            out.append({"name": rec["name"], "created_at": rec.get("created_at"),
                        "locked_until": locked_until.isoformat() if locked_until else None})
        return sorted(out, key=lambda r: r["name"].lower())

    @staticmethod
    def _recent_failures(rec: dict[str, Any]) -> list[datetime]:
        cutoff = _now() - LOCKOUT
        failures = []
        for ts in rec.get("failures", []):
            try:
                when = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if when >= cutoff:
                failures.append(when)
        return failures

    def _locked_until(self, rec: dict[str, Any]) -> datetime | None:
        failures = self._recent_failures(rec)
        if len(failures) >= MAX_FAILURES:
            return max(failures) + LOCKOUT
        return None

    # ------------------------------------------------------------------ verification
    def verify(self, name: str | None, pin: str | None) -> str:
        """Check an approver's PIN. Returns the approver's canonical name or raises PermissionDenied."""
        key = (name or "").strip().lower()
        with self._lock:
            data = self._load()
            rec = data["approvers"].get(key)
            if rec is None:
                names = ", ".join(r["name"] for r in data["approvers"].values())
                raise PermissionDenied(f"'{name or ''}' is not a registered approver (registered: {names}).")
            locked = self._locked_until(rec)
            if locked:
                raise PermissionDenied(f"Approver '{rec['name']}' is locked after repeated wrong PINs until "
                                       f"{locked.strftime('%H:%M UTC')}.")
            expected = rec["hash"]
            actual = _hash(pin or "", rec["salt"], int(rec.get("iterations", ITERATIONS)))
            if not pin or not hmac.compare_digest(expected, actual):
                rec["failures"] = [f.isoformat() for f in self._recent_failures(rec)] + [_now().isoformat()]
                self._save(data)
                self._log(rec["name"], "approver.pin_failed", "denied")
                left = max(0, MAX_FAILURES - len(rec["failures"]))
                raise PermissionDenied(f"Wrong or missing PIN for approver '{rec['name']}' "
                                       f"({left} attempt(s) left before a {int(LOCKOUT.seconds / 60)}-minute lock).")
            if rec.get("failures"):
                rec["failures"] = []
                self._save(data)
            return rec["name"]

    # ------------------------------------------------------------------ management
    @staticmethod
    def _check_pin(pin: str) -> None:
        if len(pin or "") < MIN_PIN_LENGTH:
            raise HarnessError(f"The PIN must be at least {MIN_PIN_LENGTH} characters.")

    def add(self, name: str, pin: str, *, authorized_by: str | None = None, authorizer_pin: str | None = None) -> None:
        name = (name or "").strip()
        if not is_human_actor(name) or len(name) > 80:
            raise HarnessError(f"'{name}' cannot be an approver name (use a person's name or login).")
        self._check_pin(pin)
        with self._lock:
            data = self._load()
            if data["approvers"]:
                if not authorized_by:
                    raise PermissionDenied("An existing approver must authorise adding a new approver.")
                self.verify(authorized_by, authorizer_pin)
            key = name.lower()
            if key in data["approvers"]:
                raise HarnessError(f"Approver '{name}' already exists (use change-pin).")
            salt = secrets.token_hex(16)
            data["approvers"][key] = {"name": name, "salt": salt, "iterations": ITERATIONS,
                                      "hash": _hash(pin, salt, ITERATIONS), "created_at": _now().isoformat(),
                                      "failures": []}
            self._save(data)
        self._log(name, "approver.added", authorized_by=authorized_by or "(first approver)")

    def change_pin(self, name: str, old_pin: str, new_pin: str) -> None:
        canonical = self.verify(name, old_pin)
        self._check_pin(new_pin)
        with self._lock:
            data = self._load()
            rec = data["approvers"][canonical.lower()]
            rec["salt"] = secrets.token_hex(16)
            rec["iterations"] = ITERATIONS
            rec["hash"] = _hash(new_pin, rec["salt"], ITERATIONS)
            self._save(data)
        self._log(canonical, "approver.pin_changed")

    def remove(self, name: str, *, authorized_by: str, authorizer_pin: str) -> None:
        authorizer = self.verify(authorized_by, authorizer_pin)
        with self._lock:
            data = self._load()
            if data["approvers"].pop((name or "").strip().lower(), None) is None:
                raise HarnessError(f"Approver '{name}' does not exist.")
            self._save(data)
        self._log(name, "approver.removed", authorized_by=authorizer)
