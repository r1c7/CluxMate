"""ConfigManager — persistent model-entry configuration.

Config lives at ~/.cluxmate/config.json (schema v2):

    {
      "version": 2,
      "models": [
        {"id", "api_type", "provider", "base_url", "api_key",
         "model_name", "context_1m", "max_tokens"}
      ],
      "active_model_id": "<id>"
    }

`api_type` is the API family ("openai" — OpenAI-compatible; historically also
"anthropic", now unused). `provider` is a free-text vendor label ("DeepSeek",
"OpenAI", ...). Older configs used a fixed {providers: {...}, default_provider}
shape; _migrate() converts them in place on load so existing users aren't
broken.
"""

import copy
import os
import json
import uuid
from pathlib import Path
from typing import Any


# api_key env fallback, keyed on the provider label (lowercased). Applied only
# in the build path (get_model / get_active_model), never when listing for the
# CRUD editor — the editor shows/edits the raw stored key.
_ENV_KEY_MAP = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Seeded when no config file exists yet. Ids are stable, human-readable slugs so
# the Python and TS (desktop) migrators/seeds agree. Reasoning levels are NOT
# part of the entry — they are a runtime composer choice (see the provider's
# reasoning mapping), not a config fact.
_DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "id": "deepseek", "api_type": "openai", "provider": "DeepSeek",
        "base_url": "https://api.deepseek.com", "api_key": "",
        "model_name": "deepseek-v4-flash", "context_1m": False,
    },
    {
        "id": "openai", "api_type": "openai", "provider": "OpenAI",
        "base_url": "https://api.openai.com/v1", "api_key": "",
        "model_name": "gpt-5.1", "context_1m": False,
    },
]

_ENTRY_FIELDS = (
    "id", "api_type", "provider", "base_url", "api_key", "model_name",
    "context_1m", "max_tokens", "reasoning_efforts",
)


class ConfigManager:
    """Manages ~/.cluxmate/config.json (model-entry list + active model)."""

    def __init__(self):
        self._dir = Path.home() / ".cluxmate"
        self._path = self._dir / "config.json"
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = self._fresh()
        else:
            self._data = self._fresh()
        if self._migrate():
            self.save()
        # Backstop: ensure required keys always exist.
        self._data.setdefault("version", 2)
        self._data.setdefault("models", [])
        if "active_model_id" not in self._data:
            models = self._data["models"]
            self._data["active_model_id"] = models[0]["id"] if models else ""

    def _fresh(self) -> dict[str, Any]:
        models = copy.deepcopy(_DEFAULT_MODELS)
        return {"version": 2, "models": models, "active_model_id": models[0]["id"]}

    def _migrate(self) -> bool:
        """Convert the legacy {providers, default_provider} schema to v2.

        Returns True if anything changed (so the caller persists). Idempotent:
        once "models" exists we never re-migrate.
        """
        if "models" in self._data:
            return False
        providers = self._data.get("providers")
        if not isinstance(providers, dict):
            return False

        labels = {"deepseek": "DeepSeek", "openai": "OpenAI"}
        models: list[dict[str, Any]] = []
        for name, cfg in providers.items():
            cfg = cfg if isinstance(cfg, dict) else {}
            models.append({
                "id": name,
                "api_type": "openai",
                "provider": labels.get(name, name.title()),
                "base_url": cfg.get("base_url", ""),
                "api_key": cfg.get("api_key", ""),
                "model_name": cfg.get("model", ""),
                "context_1m": False,
                "max_tokens": 0,
            })
        active = self._data.get("default_provider", "")
        if not any(m["id"] == active for m in models):
            active = models[0]["id"] if models else ""
        self._data = {"version": 2, "models": models, "active_model_id": active}
        return True

    def save(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8"
        )

    # ── reads ──────────────────────────────────────────────

    def list_models(self) -> list[dict[str, Any]]:
        """All entries, raw (api_key NOT env-resolved) — for the CRUD editor."""
        return [dict(m) for m in self._data.get("models", [])]

    def _find(self, model_id: str) -> dict[str, Any] | None:
        for m in self._data.get("models", []):
            if m.get("id") == model_id:
                return m
        return None

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        """An entry with api_key resolved from env when empty — for building."""
        m = self._find(model_id)
        if m is None:
            return None
        entry = dict(m)
        if not entry.get("api_key"):
            env_key = _ENV_KEY_MAP.get(str(entry.get("provider", "")).lower())
            if env_key:
                entry["api_key"] = os.environ.get(env_key, "")
        return entry

    def get_active_model_id(self) -> str:
        return self._data.get("active_model_id", "")

    def get_active_model(self) -> dict[str, Any] | None:
        return self.get_model(self.get_active_model_id())

    # ── writes ─────────────────────────────────────────────

    def set_active_model(self, model_id: str):
        self._data["active_model_id"] = model_id
        self.save()

    def add_model(self, entry: dict[str, Any]) -> str:
        model_id = entry.get("id") or "m_" + uuid.uuid4().hex[:12]
        clean = self._normalize(entry, model_id)
        models = self._data.setdefault("models", [])
        models.append(clean)
        if not self._data.get("active_model_id"):
            self._data["active_model_id"] = model_id
        self.save()
        return model_id

    def update_model(self, model_id: str, fields: dict[str, Any]):
        m = self._find(model_id)
        if m is None:
            return
        for k in _ENTRY_FIELDS:
            if k == "id":
                continue
            if k in fields:
                m[k] = fields[k]
        self.save()

    def delete_model(self, model_id: str):
        models = self._data.get("models", [])
        self._data["models"] = [m for m in models if m.get("id") != model_id]
        if self._data.get("active_model_id") == model_id:
            remaining = self._data["models"]
            self._data["active_model_id"] = remaining[0]["id"] if remaining else ""
        self.save()

    def _normalize(self, entry: dict[str, Any], model_id: str) -> dict[str, Any]:
        efforts = entry.get("reasoning_efforts")
        if not isinstance(efforts, list):
            efforts = []
        else:
            efforts = [str(v) for v in efforts if str(v).strip()]
        return {
            "id": model_id,
            "api_type": entry.get("api_type", "openai"),
            "provider": entry.get("provider", ""),
            "base_url": entry.get("base_url", ""),
            "api_key": entry.get("api_key", ""),
            "model_name": entry.get("model_name", ""),
            "context_1m": bool(entry.get("context_1m", False)),
            "max_tokens": entry.get("max_tokens", 0) or 0,
            "reasoning_efforts": efforts,
        }
