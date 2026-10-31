"""Dashboard branding: the name and logo shown in the top navigation bar.

* Defaults come from the settings - ``dashboard.brand_name``, ``dashboard.brand_subtitle`` and
  ``dashboard.logo`` (or ``DASHBOARD_BRAND_NAME`` / ``DASHBOARD_LOGO`` in ``.env``).
* The dashboard's **Name & logo** page saves changes in ``data/branding/``. They take precedence over
  the defaults until someone clicks *Reset to defaults*.

Uploaded logos are checked before they are stored: real image content (PNG, JPEG, GIF, WebP or SVG,
recognised by the file's bytes, not its name), at most 1 MB, and SVG files without scripts, event
handlers or embedded HTML. The dashboard serves the logo with a sandboxing Content-Security-Policy, so
even a crafted file cannot run code in the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .store import now_iso

log = logging.getLogger(__name__)

MAX_LOGO_BYTES = 1_000_000
MAX_NAME = 60
MAX_SUBTITLE = 80
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
              "image/svg+xml": ".svg"}
UNSAFE_SVG = re.compile(rb"<\s*script|javascript\s*:|\bon[a-z]+\s*=|<\s*foreignobject|<!entity|<\s*iframe|"
                        rb"<\s*embed|<\s*object", re.IGNORECASE)
BIDI_CONTROLS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}  # text-direction spoofing


class BrandingError(HarnessError):
    """An invalid name or logo."""


def sniff_image(data: bytes) -> str | None:
    """Content type from the file's bytes (never trust the file name or the browser)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<?xml", b"<svg", b"<!--", b"<!doctype svg")) and b"<svg" in data[:8192].lower():
        return "image/svg+xml"
    return None


def check_logo(data: bytes) -> str:
    """Return the logo's content type, or raise :class:`BrandingError` with a plain explanation."""
    if not data:
        raise BrandingError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingError(f"The logo is too large ({len(data) // 1024:,} KB). Please use an image up to 1 MB.")
    kind = sniff_image(data)
    if kind is None:
        raise BrandingError("That file is not a PNG, JPG, GIF, WebP or SVG image.")
    if kind == "image/svg+xml" and UNSAFE_SVG.search(data):
        raise BrandingError("This SVG contains scripts or embedded content, which is not allowed. "
                            "Save it again as a plain SVG, or use a PNG.")
    return kind


def clean_text(value: Any, limit: int, label: str, *, required: bool) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" and ch not in BIDI_CONTROLS)
    text = " ".join(text.split())
    if required and not text:
        raise BrandingError(f"Please enter a {label}.")
    if len(text) > limit:
        raise BrandingError(f"The {label} can be at most {limit} characters.")
    return text


def initial_of(name: str) -> str:
    return next((ch for ch in name if ch.isalnum()), "?").upper()


@dataclass
class Brand:
    name: str
    subtitle: str
    initial: str            # shown in the coloured square when there is no logo
    logo_url: str | None    # /branding/logo?v=<content hash>, or None
    logo_source: str        # "uploaded", "settings" or "" (no logo)
    custom: bool            # changed on the Name & logo page
    updated_by: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrandingStore:
    """Reads and changes the dashboard's name and logo (``data/branding/``)."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.dir = Path(settings.data_path) / "branding"
        self.file = self.dir / "branding.json"
        self._lock = threading.Lock()
        self._cache: tuple[tuple, Brand, tuple[bytes, str] | None] | None = None
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ reading
    def _default_logo_file(self) -> Path | None:
        ref = (self.settings.dashboard.logo or "").strip()
        if not ref:
            return None
        path = Path(self.settings.path(ref))
        if not path.is_file():
            self._warn(ref, f"Logo file {path} (dashboard.logo / DASHBOARD_LOGO) was not found.")
            return None
        return path

    def _warn(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)

    def _saved(self) -> dict[str, Any]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self._warn(str(self.file), f"Ignoring unreadable {self.file}: {exc}")
            return {}

    def _stamp(self) -> tuple:
        """Changes whenever anything that affects the brand changes (a few cheap stat calls)."""
        parts = []
        for path in (self.file, self._default_logo_file()):
            try:
                st = path.stat() if path is not None else None
            except OSError:
                st = None
            parts.append((str(path), st.st_mtime_ns, st.st_size) if st else None)
        d = self.settings.dashboard
        return (*parts, d.brand_name, d.brand_subtitle, d.logo)

    def _read_logo(self, path: Path, what: str) -> tuple[bytes, str] | None:
        try:
            data = path.read_bytes()
            return data, check_logo(data)
        except (OSError, BrandingError) as exc:
            self._warn(f"{what}:{path}", f"{what} logo {path} cannot be used: {exc}")
            return None

    def _load(self) -> tuple[Brand, tuple[bytes, str] | None]:
        stamp = self._stamp()
        cached = self._cache
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]
        saved = self._saved()
        d = self.settings.dashboard
        name = saved.get("name") or d.brand_name or "Redmine Agent"
        subtitle = saved["subtitle"] if "subtitle" in saved else (d.brand_subtitle or "")
        logo, source = None, ""
        if saved.get("logo_file"):
            logo = self._read_logo(self.dir / str(saved["logo_file"]), "Uploaded")
            source = "uploaded" if logo else ""
        elif not saved.get("logo_removed"):
            default = self._default_logo_file()
            logo = self._read_logo(default, "Configured") if default else None
            source = "settings" if logo else ""
        url = f"/branding/logo?v={hashlib.sha256(logo[0]).hexdigest()[:12]}" if logo else None
        brand = Brand(name=name, subtitle=subtitle, initial=initial_of(name), logo_url=url, logo_source=source,
                      custom=bool(saved), updated_by=str(saved.get("updated_by", "")),
                      updated_at=str(saved.get("updated_at", "")))
        self._cache = (stamp, brand, logo)
        return brand, logo

    def current(self) -> Brand:
        return self._load()[0]

    def logo(self) -> tuple[bytes, str] | None:
        """(image bytes, content type) of the logo in use, or None."""
        return self._load()[1]

    # ------------------------------------------------------------------ changing
    def _save(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.file)
        self._cache = None

    def update(self, *, name: Any = None, subtitle: Any = None, actor: str) -> Brand:
        with self._lock:
            data = self._saved()
            if name is not None:
                data["name"] = clean_text(name, MAX_NAME, "name", required=True)
            if subtitle is not None:
                data["subtitle"] = clean_text(subtitle, MAX_SUBTITLE, "subtitle", required=False)
            data.update(updated_by=actor, updated_at=now_iso())
            self._save(data)
        return self.current()

    def set_logo(self, data: bytes, *, actor: str) -> Brand:
        kind = check_logo(data)
        with self._lock:
            saved = self._saved()
            self.dir.mkdir(parents=True, exist_ok=True)
            filename = "logo" + EXTENSIONS[kind]
            tmp = self.dir / (filename + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self.dir / filename)
            old = saved.get("logo_file")
            if old and old != filename:
                (self.dir / str(old)).unlink(missing_ok=True)
            saved.pop("logo_removed", None)
            saved.update(logo_file=filename, logo_type=kind, updated_by=actor, updated_at=now_iso())
            self._save(saved)
        return self.current()

    def remove_logo(self, *, actor: str) -> Brand:
        """Show the coloured initial instead of a logo (also hides a logo set in the settings)."""
        with self._lock:
            saved = self._saved()
            old = saved.pop("logo_file", None)
            saved.pop("logo_type", None)
            if old:
                (self.dir / str(old)).unlink(missing_ok=True)
            saved.update(logo_removed=True, updated_by=actor, updated_at=now_iso())
            self._save(saved)
        return self.current()

    def reset(self) -> Brand:
        """Back to the defaults from the settings / .env."""
        with self._lock:
            saved = self._saved()
            if saved.get("logo_file"):
                (self.dir / str(saved["logo_file"])).unlink(missing_ok=True)
            self.file.unlink(missing_ok=True)
            self._cache = None
        return self.current()
