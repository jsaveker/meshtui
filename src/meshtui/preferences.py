"""Small, local-only operator preferences.

The public repository contains the available layouts and themes; the choice an
operator makes lives under their XDG config directory and is scoped to the
active radio protocol.  Nothing about a particular station, host, or home is
stored in the project checkout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LAYOUTS = ("balanced", "radio", "chat", "route")
THEMES = ("phosphor", "night-vision", "blue-noir", "high-contrast")


def default_preferences_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "meshtui" / "preferences.json"


class OperatorPreferences:
    """Read and atomically update protocol-scoped display preferences."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self.data: dict[str, Any] = {"version": 1, "protocols": {}}
        self.load()

    def load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        if isinstance(value, dict) and isinstance(value.get("protocols"), dict):
            self.data = value

    def get(self, protocol: str) -> dict[str, str]:
        raw = self.data.get("protocols", {}).get(protocol, {})
        raw = raw if isinstance(raw, dict) else {}
        layout = raw.get("layout", "balanced")
        theme = raw.get("theme", "phosphor")
        return {
            "layout": layout if layout in LAYOUTS else "balanced",
            "theme": theme if theme in THEMES else "phosphor",
        }

    def update(self, protocol: str, **values: str) -> None:
        raw = self.data.get("protocols", {}).get(protocol, {})
        current = dict(raw) if isinstance(raw, dict) else {}
        if values.get("layout") in LAYOUTS:
            current["layout"] = values["layout"]
        if values.get("theme") in THEMES:
            current["theme"] = values["theme"]
        protocols = self.data.setdefault("protocols", {})
        protocols[protocol] = current
        self._save()

    def views(self, protocol: str) -> dict[str, str]:
        raw = self.data.get("protocols", {}).get(protocol, {})
        views = raw.get("views", {}) if isinstance(raw, dict) else {}
        return {str(name): str(expression) for name, expression in views.items()
                if isinstance(name, str) and isinstance(expression, str)} \
            if isinstance(views, dict) else {}

    def save_view(self, protocol: str, name: str, expression: str) -> None:
        protocols = self.data.setdefault("protocols", {})
        raw = protocols.setdefault(protocol, {})
        views = raw.setdefault("views", {})
        views[name] = expression
        self._save()

    def delete_view(self, protocol: str, name: str) -> bool:
        protocols = self.data.setdefault("protocols", {})
        raw = protocols.setdefault(protocol, {})
        views = raw.setdefault("views", {})
        if name not in views:
            return False
        del views[name]
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
