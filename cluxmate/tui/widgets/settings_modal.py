"""Settings screen — keyboard-first form.

Use Tab/Shift+Tab to navigate, Enter on inputs to edit, arrows for Select.
Esc to go back.  No mouse interaction required.
"""

import uuid

from textual.app import ComposeResult
from textual.containers import VerticalScroll, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static
from textual.binding import Binding


API_TYPES = [("OpenAI-style API", "openai")]


class SettingsScreen(Screen):
    """Full-screen settings with keyboard navigation."""

    AUTO_FOCUS = None

    CSS = """
    #settings-header { text-style: bold; text-align: center; height: 1; margin-bottom: 1; }
    .section-label { text-style: bold; height: 1; margin-top: 1; }
    .provider-header { text-style: bold; color: $accent; margin-top: 1; }
    .entry-select { margin: 1 0; }
    .entry-input { height: 3; }
    .entry-checkbox { margin: 1 0; }
    .entry-del-btn { margin: 1 0; min-width: 14; }
    #model-list { height: auto; }
    Static { height: auto; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, config_data: dict):
        super().__init__()
        self._models: list[dict] = [dict(m) for m in config_data.get("models", [])]
        for m in self._models:
            if not m.get("id"):
                m["id"] = "m_" + uuid.uuid4().hex[:12]
        self._active = config_data.get("active_model_id", "")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Settings  —  Esc to go back  —  Ctrl+S to save",
                         id="settings-header")
            yield Static("")
            yield Static("Active Model", classes="section-label")
            yield Select(
                options=self._active_options(), value=self._active_value(),
                id="active-model", allow_blank=False,
            )
            yield Static("")
            yield Static("Models", classes="section-label")
            yield Vertical(id="model-list")
            yield Button("+ Add Model", id="btn-add-model")
            yield Static("")
            with Horizontal():
                yield Button("Save", variant="primary", id="btn-save-settings")
                yield Button("Cancel", id="btn-cancel-settings")
            yield Static("", id="settings-msg")

    def on_mount(self):
        self._render_entries()

    # ── actions ────────────────────────────────────────────

    def action_cancel(self):
        self.dismiss(None)

    def action_save(self):
        self._save()

    # ── helpers ────────────────────────────────────────────

    def _active_options(self):
        return [(self._entry_label(m), m["id"]) for m in self._models] or [
            ("(no models)", "")
        ]

    def _active_value(self):
        ids = {m["id"] for m in self._models}
        return self._active if self._active in ids else (
            self._models[0]["id"] if self._models else ""
        )

    @staticmethod
    def _entry_label(m: dict) -> str:
        return f"{m.get('provider','?')} / {m.get('model_name','?')}"

    # ── render ─────────────────────────────────────────────

    def _render_entries(self):
        container = self.query_one("#model-list", Vertical)
        container.remove_children()
        for m in self._models:
            self._mount_entry(m, container)

    def _mount_entry(self, m: dict, container: Vertical):
        mid = m["id"]
        container.mount(Static(
            f"— {self._entry_label(m)} —", classes="provider-header",
        ))
        container.mount(Label("API Type"))
        container.mount(Select(
            options=API_TYPES, value=m.get("api_type", "openai"),
            id=f"type-{mid}", allow_blank=False, classes="entry-select",
        ))
        container.mount(Label("Provider (label)"))
        container.mount(Input(
            value=m.get("provider", ""), id=f"provider-{mid}", classes="entry-input",
        ))
        container.mount(Label("Base URL"))
        container.mount(Input(
            value=m.get("base_url", ""), id=f"url-{mid}", classes="entry-input",
        ))
        container.mount(Label("API Key"))
        container.mount(Input(
            value=m.get("api_key", ""), password=True, id=f"key-{mid}",
            classes="entry-input",
        ))
        container.mount(Label("Model Name"))
        container.mount(Input(
            value=m.get("model_name", ""), id=f"model-{mid}", classes="entry-input",
        ))
        container.mount(Checkbox(
            "Supports 1M context", value=bool(m.get("context_1m", False)),
            id=f"ctx1m-{mid}", classes="entry-checkbox",
        ))
        container.mount(Label("Max Output Tokens (0 = auto)"))
        container.mount(Input(
            value=str(m.get("max_tokens") or ""), id=f"maxtok-{mid}", classes="entry-input",
        ))
        container.mount(Button(
            "Delete", variant="error", id=f"del-{mid}", classes="entry-del-btn",
        ))

    # ── buttons ────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id or ""
        if bid == "btn-save-settings":
            self._save()
        elif bid == "btn-cancel-settings":
            self.dismiss(None)
        elif bid == "btn-add-model":
            self._add_entry()
        elif bid.startswith("del-"):
            self._delete_entry(bid[4:])

    # ── data ───────────────────────────────────────────────

    def _add_entry(self):
        self._sync_from_widgets()
        self._models.append({
            "id": "m_" + uuid.uuid4().hex[:12],
            "api_type": "openai", "provider": "New Model",
            "base_url": "", "api_key": "", "model_name": "",
            "context_1m": False,
            "max_tokens": 0,
        })
        self._render_entries()
        self._refresh_active_options()

    def _delete_entry(self, mid: str):
        self._sync_from_widgets()
        self._models = [m for m in self._models if m["id"] != mid]
        if self._active == mid:
            self._active = self._models[0]["id"] if self._models else ""
        self._render_entries()
        self._refresh_active_options()

    def _refresh_active_options(self):
        sel = self.query_one("#active-model", Select)
        sel.set_options(self._active_options())
        sel.value = self._active_value()

    def _sync_from_widgets(self):
        for m in self._models:
            mid = m["id"]
            try:
                m["api_type"] = self.query_one(f"#type-{mid}", Select).value
                m["provider"] = self.query_one(f"#provider-{mid}", Input).value
                m["base_url"] = self.query_one(f"#url-{mid}", Input).value
                m["api_key"] = self.query_one(f"#key-{mid}", Input).value
                m["model_name"] = self.query_one(f"#model-{mid}", Input).value
                m["context_1m"] = self.query_one(f"#ctx1m-{mid}", Checkbox).value
                try:
                    raw = self.query_one(f"#maxtok-{mid}", Input).value
                    m["max_tokens"] = int(raw) if raw.strip() else 0
                except Exception:
                    m["max_tokens"] = 0
            except Exception:
                pass

    def _save(self):
        self._sync_from_widgets()
        active = self.query_one("#active-model", Select).value
        self.dismiss({
            "models": self._models,
            "active_model_id": active or (
                self._models[0]["id"] if self._models else ""
            ),
        })
