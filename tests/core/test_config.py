"""Tests for ConfigManager — v2 model-entry schema + legacy migration."""

import json
from pathlib import Path

import pytest

from cluxmate.core.config import ConfigManager


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect Path.home() so ConfigManager writes to a throwaway dir."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _write_config(home: Path, data: dict):
    d = home / ".cluxmate"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data), "utf-8")


def test_fresh_seeds_default_models(home):
    cm = ConfigManager()
    ids = {m["id"] for m in cm.list_models()}
    assert ids == {"deepseek", "openai"}
    assert cm.get_active_model_id() == "deepseek"


def test_migrates_legacy_schema(home):
    _write_config(home, {
        "providers": {
            "deepseek": {"api_key": "dsk", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
            "openai": {"api_key": "", "base_url": "https://api.openai.com/v1", "model": "gpt-5.1"},
        },
        "default_provider": "openai",
    })
    cm = ConfigManager()

    models = {m["id"]: m for m in cm.list_models()}
    assert models["deepseek"]["api_type"] == "openai"
    assert models["deepseek"]["provider"] == "DeepSeek"
    assert models["deepseek"]["model_name"] == "deepseek-v4-flash"
    assert models["deepseek"]["api_key"] == "dsk"
    assert all(m["context_1m"] is False for m in models.values())
    assert cm.get_active_model_id() == "openai"


def test_migration_persisted_and_idempotent(home):
    _write_config(home, {
        "providers": {"openai": {"api_key": "k", "base_url": "u", "model": "gpt-5.1"}},
        "default_provider": "openai",
    })
    ConfigManager()  # migrates + saves
    raw = json.loads((home / ".cluxmate" / "config.json").read_text("utf-8"))
    assert "models" in raw and "providers" not in raw
    assert raw["version"] == 2

    # Second load must not re-migrate or change anything.
    cm2 = ConfigManager()
    assert [m["id"] for m in cm2.list_models()] == ["openai"]


def test_migration_bad_default_falls_back_to_first(home):
    _write_config(home, {
        "providers": {"openai": {"api_key": "", "base_url": "", "model": "x"}},
        "default_provider": "deepseek",  # not present in providers
    })
    cm = ConfigManager()
    assert cm.get_active_model_id() == "openai"


def test_add_update_delete(home):
    cm = ConfigManager()
    mid = cm.add_model({
        "api_type": "openai", "provider": "Local", "base_url": "http://localhost:8000",
        "api_key": "secret", "model_name": "llama", "context_1m": True,
    })
    assert mid.startswith("m_")
    entry = next(m for m in cm.list_models() if m["id"] == mid)
    assert entry["provider"] == "Local" and entry["context_1m"] is True

    cm.update_model(mid, {"model_name": "llama-3", "context_1m": False})
    entry = next(m for m in cm.list_models() if m["id"] == mid)
    assert entry["model_name"] == "llama-3" and entry["context_1m"] is False

    cm.delete_model(mid)
    assert all(m["id"] != mid for m in cm.list_models())


def test_delete_active_reassigns(home):
    cm = ConfigManager()
    cm.set_active_model("openai")
    assert cm.get_active_model_id() == "openai"
    cm.delete_model("openai")
    # Reassigned to the first remaining entry.
    remaining = [m["id"] for m in cm.list_models()]
    assert cm.get_active_model_id() in remaining
    assert cm.get_active_model_id() != "openai"


def test_delete_last_model_clears_active(home):
    _write_config(home, {"version": 2, "models": [
        {"id": "only", "api_type": "openai", "provider": "P", "base_url": "",
         "api_key": "k", "model_name": "m", "context_1m": False},
    ], "active_model_id": "only"})
    cm = ConfigManager()
    cm.delete_model("only")
    assert cm.get_active_model_id() == ""
    assert cm.list_models() == []


def test_env_fallback_only_in_build_path(home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    cm = ConfigManager()  # deepseek entry has empty api_key

    # list_models() returns the RAW stored key (empty) — the editor must not see env.
    raw = next(m for m in cm.list_models() if m["id"] == "deepseek")
    assert raw["api_key"] == ""

    # get_model() resolves it from env for building.
    resolved = cm.get_model("deepseek")
    assert resolved["api_key"] == "from-env"

    # A stored key is NOT overridden by env.
    cm.update_model("deepseek", {"api_key": "stored"})
    assert cm.get_model("deepseek")["api_key"] == "stored"


def test_get_model_missing_returns_none(home):
    cm = ConfigManager()
    assert cm.get_model("nope") is None


def test_first_add_to_empty_becomes_active(home):
    _write_config(home, {"version": 2, "models": [], "active_model_id": ""})
    cm = ConfigManager()
    mid = cm.add_model({"api_type": "openai", "provider": "P", "base_url": "",
                        "api_key": "k", "model_name": "m", "context_1m": False})
    assert cm.get_active_model_id() == mid
