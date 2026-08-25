"""CluxMate TUI — Textual-based terminal interface.

Avoids ``Screen`` overlays entirely — push_screen / dismiss are unreliable in
some terminal emulators (Git Bash / MINGW64).  Instead, all UI modes live in
the main App body as containers that are shown/hidden with ``.display``.
"""

import asyncio
import os

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Footer, Header, Input, Static, Label, Select, Checkbox,
)
from textual.binding import Binding

from .controller import TuiController
from .widgets.chat_view import ChatView
from .widgets.input_box import InputBox
from .widgets.session_list import SessionList
from cluxmate.core.reasoning import options_for

# ── constants ──────────────────────────────────────────────────────────────

PERMISSION_MODES = ["plan", "default", "acceptEdits", "yolo"]
PERMISSION_LABELS: dict[str, str] = {
    "plan": "Plan (read-only)",
    "default": "Default",
    "acceptEdits": "Accept Edits",
    "yolo": "Yolo (auto-all)",
}

API_TYPES = [("OpenAI-style API", "openai")]

# ── App ────────────────────────────────────────────────────────────────────


class CluxMateApp(App):
    """CluxMate terminal UI — keyboard-first, no overlay screens."""

    CSS = """
    #body { height: 1fr; }
    #sidebar { width: 24; height: 100%; border: solid $primary; }
    #main-area { width: 1fr; height: 100%; }
    #chat-panel { height: 100%; }
    #chat-view { height: 1fr; }
    #chat-log { height: 1fr; padding: 0 1; }
    #thinking-indicator { height: 1; padding: 0 1; }
    #input-row { height: auto; padding: 1 0 1 0; border-top: solid $surface; }
    #input-box { height: auto; padding: 0 0 1 0; }
    #prompt-input { width: 100%; height: 3; }
    #status-bar { height: 1; padding: 0 1; background: $panel; }
    #mode-row { height: auto; padding: 0 1; }
    #mode-row Button { height: 1; min-height: 1; padding: 0 2; }
    #mode-note { height: 1; }
    .dim { color: $text-disabled; }
    .section-header { padding: 1; background: $panel; text-style: bold; }
    #session-items { height: 1fr; }
    .session-btn { width: 100%; text-align: left; }
    #session-actions { height: auto; padding-top: 1; border-top: solid $surface; }
    #session-actions Button { width: 100%; }
    /* ── settings panel ── */
    #settings-panel { display: none; height: 100%; padding: 0 2 2 2; }
    #settings-header { text-style: bold; text-align: center; height: 1; margin-bottom: 1; }
    .section-label { text-style: bold; height: 1; margin-top: 1; }
    .provider-header { text-style: bold; color: $accent; margin-top: 1; }
    .entry-select { margin: 1 0; }
    .entry-input { height: 3; }
    .entry-checkbox { margin: 1 0; }
    .entry-del-btn { margin: 1 0; min-width: 14; }
    #settings-actions { height: auto; }
    #settings-msg { height: 1; }
    #model-list { height: auto; }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_session", "New Session", key_display="^N"),
        Binding("ctrl+d", "delete_session", "Delete Session", key_display="^D"),
        Binding("ctrl+s", "toggle_settings", "Settings", key_display="^S"),
        Binding("ctrl+o", "change_cwd", "Working Dir", key_display="^O"),
        Binding("escape", "focus_input", "Focus Input", key_display="Esc"),
        Binding("ctrl+m", "cycle_mode", "Cycle Mode", key_display="^M"),
        Binding("ctrl+t", "cycle_model", "Cycle Model", key_display="^T"),
        Binding("ctrl+r", "cycle_effort", "Cycle Effort", key_display="^R"),
        Binding("ctrl+j", "next_session", "Next Session"),
        Binding("ctrl+k", "prev_session", "Prev Session"),
    ]

    # ── init / compose ───────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self.ctrl = TuiController()
        self._history: list[dict] = []
        self._first_setup = False
        self._permission_mode = "default"
        self._cwd = os.getcwd()
        self._pending_cwd_change = False
        self._session_order: list[str] = []
        # Settings state — all live in the app, no Screen needed.
        self._settings_visible = False
        self._sm_models: list[dict] = []
        self._sm_active = ""
        # Inline tool-approval state. While a write/dangerous tool waits for
        # the user in non-auto-approve modes, the agent loop blocks on this
        # future; the next input submission resolves it with y/n.
        self._approval_future: asyncio.Future | None = None
        self._approval_tool_name: str = ""
        # Inline question state (ask_user_question tool). While a question waits,
        # the agent loop blocks on this future; the next input submission resolves
        # it with the raw answer text.
        self._question_future: asyncio.Future | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield SessionList(id="sidebar")
            with Container(id="main-area"):
                # ── chat view (default) ──
                with Vertical(id="chat-panel"):
                    yield ChatView(id="chat-view")
                    with Horizontal(id="mode-row"):
                        yield Button(
                            "Model: ?", id="btn-cycle-model", variant="default",
                            compact=True,
                        )
                        yield Button(
                            "推理: ?", id="btn-cycle-effort", variant="default",
                            compact=True,
                        )
                        yield Button(
                            PERMISSION_LABELS["default"], id="btn-cycle-mode",
                            variant="default",
                            # compact removes the 1-cell border (default Button
                            # is height 3 with a tall border). Without it a
                            # forced height:1 leaves 0 content rows and the
                            # label is invisible — same bug as Input.
                            compact=True,
                        )
                        yield Static("", id="mode-note")
                    yield InputBox(id="input-box")

                # ── settings panel (hidden) ──
                with VerticalScroll(id="settings-panel"):
                    yield Static(
                        "Settings  —  Ctrl+S back to chat  —  Esc focus input",
                        id="settings-header",
                    )
                    yield Static("")
                    yield Static("Active Model", classes="section-label")
                    yield Select(
                        [("(no models)", "")], id="settings-active-model",
                        allow_blank=False, value="",
                    )
                    yield Static("")
                    yield Static("Models", classes="section-label")
                    yield Vertical(id="model-list")
                    yield Button("+ Add Model", id="btn-add-model")
                    yield Static("")
                    with Horizontal(id="settings-actions"):
                        yield Button(
                            "Save & Return", variant="primary",
                            id="btn-save-settings",
                        )
                        yield Button("Cancel", id="btn-cancel-settings")
                    yield Static("", id="settings-msg")
        yield Footer()

    async def on_mount(self):
        # Clean up empty sessions — sessions whose only message is the
        # initial system/assistant echo (no actual user input).
        self._clean_empty_sessions()

        has_key = any(
            (self.ctrl.config.get_model(m["id"]) or {}).get("api_key")
            for m in self.ctrl.config.list_models()
        )
        if has_key:
            self._prompt_new_session()
        else:
            self._first_setup = True
            self.query_one(ChatView).add_info(
                "No API key. Press Ctrl+S to open Settings."
            )
            await self._open_settings_view()
        self._update_mode_button()
        self._update_status()

    # ── actions ──────────────────────────────────────────────────────────

    def action_new_session(self):
        self._prompt_new_session()

    def action_delete_session(self):
        sid = self.ctrl.active_session_id
        if sid is None:
            return
        self.ctrl.delete_session(sid)
        self._history = []
        self._refresh_session_list()
        self.query_one(ChatView).clear()
        self.query_one(ChatView).add_info("Session deleted.")
        self._update_status()

    def action_focus_input(self):
        self.query_one("#prompt-input").focus()

    async def action_toggle_settings(self):
        if self._settings_visible:
            self._close_settings_view()
        else:
            await self._open_settings_view()

    def action_change_cwd(self):
        self._pending_cwd_change = True
        self.query_one(ChatView).add_info(
            "[yellow]Type an absolute directory path and press Enter.[/]"
        )
        self.query_one(ChatView).add_info(
            f"  Current: [bold]{self._cwd}[/]"
        )

    def action_cycle_mode(self):
        idx = PERMISSION_MODES.index(self._permission_mode)
        self._permission_mode = PERMISSION_MODES[(idx + 1) % len(PERMISSION_MODES)]
        self.ctrl.set_mode(self._permission_mode)
        self._update_mode_button()
        self.query_one(ChatView).add_info(
            f"Mode: {PERMISSION_LABELS[self._permission_mode]}"
        )

    def action_cycle_model(self):
        ids = [m["id"] for m in self.ctrl.config.list_models() if m.get("id")]
        if not ids:
            return
        cur = self.ctrl.current_model_id()
        idx = ids.index(cur) if cur in ids else -1
        nxt = ids[(idx + 1) % len(ids)]
        self.ctrl.set_model(nxt)
        self._update_model_effort_buttons()
        self._update_status()
        entry = self.ctrl.config.get_model(nxt) or {}
        self.query_one(ChatView).add_info(
            f"Model: {entry.get('provider','?')} / {entry.get('model_name','?')}"
        )

    def action_cycle_effort(self):
        efforts = self._current_efforts()
        if not efforts:
            self.query_one(ChatView).add_info("[dim]No reasoning values.[/]")
            return
        cur = self.ctrl.current_reasoning_effort()
        idx = efforts.index(cur) if cur in efforts else -1
        nxt = efforts[(idx + 1) % len(efforts)]
        self.ctrl.set_reasoning_effort(nxt)
        self._update_model_effort_buttons()
        self._update_status()
        self.query_one(ChatView).add_info(f"Reasoning effort: {nxt}")

    def action_next_session(self):
        self._cycle_session(1)

    def action_prev_session(self):
        self._cycle_session(-1)

    # ── helpers ──────────────────────────────────────────────────────────

    def _update_mode_button(self):
        self.query_one("#btn-cycle-mode", Button).label = (
            PERMISSION_LABELS[self._permission_mode]
        )

    def _current_efforts(self):
        mid = self.ctrl.current_model_id()
        entry = self.ctrl.config.get_model(mid) if mid else None
        if entry is None:
            entry = self.ctrl.config.get_active_model()
        return options_for(entry or {})

    def _current_effort_label(self):
        return self.ctrl.current_reasoning_effort() or "—"

    def _update_model_effort_buttons(self):
        mid = self.ctrl.current_model_id()
        entry = self.ctrl.config.get_model(mid) if mid else None
        if entry is None:
            entry = self.ctrl.config.get_active_model()
        model_label = (
            f"{entry.get('provider','?')}/{entry.get('model_name','?')}"
            if entry else "No model"
        )
        self.query_one("#btn-cycle-model", Button).label = f"Model: {model_label}"
        self.query_one("#btn-cycle-effort", Button).label = f"推理: {self._current_effort_label()}"

    def _update_status(self):
        self._update_model_effort_buttons()
        sid = self.ctrl.active_session_id
        if sid:
            data = self.ctrl.get_session_data()
            if data:
                p = data.get("provider", "?")
                m = data.get("model", "?")
                self.query_one(InputBox).set_status(
                    f"Session: {sid} | {p} / {m} | 推理:{self._current_effort_label()} | {self._cwd}"
                )
                return
        self.query_one(InputBox).set_status(f"No session | {self._cwd}")

    def _cycle_session(self, delta: int):
        sids = self._session_order
        if not sids:
            return
        cur = self.ctrl.active_session_id
        idx = sids.index(cur) if cur in sids else -1
        if idx >= 0:
            self._load_session(sids[(idx + delta) % len(sids)])

    # ── buttons ──────────────────────────────────────────────────────────

    async def on_button_pressed(self, event):
        bid = event.button.id
        if not bid:
            return
        # ── chat buttons ──
        if bid == "btn-new-session":
            self._prompt_new_session()
        elif bid == "btn-delete-session":
            self.action_delete_session()
        elif bid == "btn-open-settings":
            await self._open_settings_view()
        elif bid == "btn-cycle-mode":
            self.action_cycle_mode()
        elif bid.startswith("session-btn-"):
            self._load_session(bid[len("session-btn-"):])
        # ── settings buttons ──
        elif bid == "btn-save-settings":
            self._settings_save()
        elif bid == "btn-cancel-settings":
            self._close_settings_view()
        elif bid == "btn-add-model":
            await self._settings_add_model()
        elif bid.startswith("del-"):
            await self._settings_delete_model(bid[4:])

    # ── input ────────────────────────────────────────────────────────────

    def on_input_submitted(self, event):
        text = event.value.strip()
        if not text:
            return
        # While a question is pending (ask_user_question tool), the submitted
        # text is the answer — resolve the blocked agent loop instead of sending
        # a chat message.
        if self._question_future is not None:
            fut = self._question_future
            self._question_future = None
            self.query_one(InputBox).clear_input()
            fut.set_result(text)
            return
        # While a tool-approval prompt is pending, the submitted text is the
        # y/n answer — resolve the blocked agent loop instead of sending a
        # chat message.
        if self._approval_future is not None:
            fut = self._approval_future
            self._approval_future = None
            self.query_one(InputBox).clear_input()
            ans = text.lower()
            fut.set_result(ans in ("y", "yes"))
            return
        if self._pending_cwd_change:
            self._pending_cwd_change = False
            # The submitted text was a directory path, not a message — clear
            # the input so it doesn't linger after the cwd switch.
            self.query_one(InputBox).clear_input()
            if os.path.isabs(text) and os.path.isdir(text):
                self._cwd = text
                self.query_one(ChatView).add_info(
                    f"Working dir: [bold]{self._cwd}[/]"
                )
                self._update_status()
                self._prompt_new_session()
            else:
                self.query_one(ChatView).add_info(
                    f"[red]Not a valid directory: {text}[/]"
                )
            return
        self._send_message(text)

    # ── sessions ─────────────────────────────────────────────────────────

    def _prompt_new_session(self):
        config = self.ctrl.config
        model_id = config.get_active_model_id()
        entry = config.get_model(model_id) or {}
        label = entry.get("provider", "?")
        model = entry.get("model_name", "?")

        self.ctrl.new_session(model_id, self._cwd, self._permission_mode)
        self._history = []
        self._refresh_session_list()
        chat = self.query_one(ChatView)
        chat.clear()
        chat.add_info(
            f"New session — {label} / {model}  |  {self._cwd}"
        )
        self._update_status()
        if self.ctrl._agent is None:
            chat.add_info("[red]No API key — Ctrl+S Settings[/]")

    def _load_session(self, session_id: str):
        data = self.ctrl.switch_session(
            session_id, self._permission_mode, self._cwd,
        )
        if data is None:
            self._prompt_new_session()
            return

        self._history = data.get("messages", [])
        saved_cwd = data.get("working_dir", "")
        if saved_cwd:
            self._cwd = saved_cwd

        self._refresh_session_list()
        chat = self.query_one(ChatView)
        chat.clear()
        chat.add_info(
            f"Loaded: {data.get('title','Session')}  |  "
            f"{data.get('provider','?')} — {len(self._history)} msgs"
        )
        if saved_cwd:
            chat.add_info(f"Working dir: {saved_cwd}")

        for msg in self._history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                chat.add_user_message(content)
            elif role == "assistant":
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            chat.add_agent_message(block["text"])
                elif isinstance(content, str) and content:
                    chat.add_agent_message(content)
        self._update_status()

    def _clean_empty_sessions(self):
        """Remove sessions whose history contains no user messages.

        Desktop and TUI session creation writes an empty ``"messages": []``
        entry.  Sessions that were never used (user didn't send anything)
        are noise in the sidebar — drop them on startup.
        """
        sessions = self.ctrl.list_sessions()
        deleted = 0
        for s in sessions:
            sid = s.get("id", "")
            if not sid:
                continue
            data = self.ctrl.sessions.load(sid)
            if data is None:
                continue
            msgs = data.get("messages") or []
            has_user = any(
                m.get("role") == "user" for m in msgs if isinstance(m, dict)
            )
            if not has_user:
                self.ctrl.sessions.delete(sid)
                deleted += 1
        if deleted:
            self.query_one(ChatView).add_info(
                f"[dim]Cleaned up {deleted} empty session(s)[/]"
            )

    # Runs as an exclusive worker in its own group so it never cancels the
    # (also exclusive, default-group) _send_message worker. update_list() is
    # async because remove_children() must be awaited to avoid DuplicateIds.
    @work(exclusive=True, group="refresh")
    async def _refresh_session_list(self):
        sessions = self.ctrl.list_sessions()
        self._session_order = [s["id"] for s in sessions if s.get("id")]
        await self.query_one(SessionList).update_list(
            sessions, self.ctrl.active_session_id,
        )

    # ── settings (inline panel, no Screen) ───────────────────────────────

    async def _open_settings_view(self):
        self._settings_visible = True
        self._sm_models = [dict(m) for m in self.ctrl.config.list_models()]
        for m in self._sm_models:
            if not m.get("id"):
                m["id"] = "m_" + __import__("uuid").uuid4().hex[:12]
        self._sm_active = self.ctrl.config.get_active_model_id()
        self._settings_refresh_active()
        await self._settings_render_entries()
        # Show settings, hide chat.
        self.query_one("#chat-panel", Vertical).display = False
        self.query_one("#settings-panel", VerticalScroll).display = True

    def _close_settings_view(self):
        self._settings_visible = False
        self.query_one("#chat-panel", Vertical).display = True
        self.query_one("#settings-panel", VerticalScroll).display = False
        if self._first_setup:
            self._first_setup = False
            self._prompt_new_session()
        self.query_one("#prompt-input").focus()

    @staticmethod
    def _sm_label(m: dict) -> str:
        return f"{m.get('provider','?')} / {m.get('model_name','?')}"

    def _settings_active_options(self):
        return [(self._sm_label(m), m["id"]) for m in self._sm_models] or [
            ("(no models)", "")
        ]

    def _settings_active_value(self):
        ids = {m["id"] for m in self._sm_models}
        return self._sm_active if self._sm_active in ids else (
            self._sm_models[0]["id"] if self._sm_models else ""
        )

    def _settings_refresh_active(self):
        sel = self.query_one("#settings-active-model", Select)
        sel.set_options(self._settings_active_options())
        sel.value = self._settings_active_value()

    async def _settings_render_entries(self):
        container = self.query_one("#model-list", Vertical)
        # remove_children() is async — it posts Prune messages and only detaches
        # widgets from the node list later on the message pump. Not awaiting it
        # makes re-mounting the same widget IDs raise DuplicateIds.
        await container.remove_children()
        for m in self._sm_models:
            self._sm_mount_entry(m, container)

    def _sm_mount_entry(self, m: dict, container: Vertical):
        mid = m["id"]
        container.mount(
            Static(f"— {self._sm_label(m)} —", classes="provider-header"),
        )
        container.mount(Label("API Type"))
        container.mount(Select(
            options=API_TYPES, value=m.get("api_type", "openai"),
            id=f"type-{mid}", allow_blank=False, classes="entry-select",
        ))
        container.mount(Label("Provider (label)"))
        container.mount(Input(
            value=m.get("provider", ""), id=f"provider-{mid}",
            classes="entry-input",
        ))
        container.mount(Label("Base URL"))
        container.mount(Input(
            value=m.get("base_url", ""), id=f"url-{mid}",
            classes="entry-input",
        ))
        container.mount(Label("API Key"))
        container.mount(Input(
            value=m.get("api_key", ""), password=True, id=f"key-{mid}",
            classes="entry-input",
        ))
        container.mount(Label("Model Name"))
        container.mount(Input(
            value=m.get("model_name", ""), id=f"model-{mid}",
            classes="entry-input",
        ))
        container.mount(Checkbox(
            "Supports 1M context", value=bool(m.get("context_1m", False)),
            id=f"ctx1m-{mid}", classes="entry-checkbox",
        ))
        container.mount(Label("Reasoning values (comma-separated, optional — overrides preset)"))
        container.mount(Input(
            value=", ".join(m.get("reasoning_efforts") or []),
            id=f"efforts-{mid}", classes="entry-input",
        ))
        container.mount(Label(
            "Tip: unsure about the reasoning_effort values? Choose default."
        ))
        container.mount(Button(
            "Delete", variant="error", id=f"del-{mid}", classes="entry-del-btn",
        ))

    # ── settings data sync ───────────────────────────────────────────────

    def _settings_sync(self):
        for m in self._sm_models:
            mid = m["id"]
            try:
                m["api_type"] = self.query_one(f"#type-{mid}", Select).value
                m["provider"] = self.query_one(f"#provider-{mid}", Input).value
                m["base_url"] = self.query_one(f"#url-{mid}", Input).value
                m["api_key"] = self.query_one(f"#key-{mid}", Input).value
                m["model_name"] = self.query_one(f"#model-{mid}", Input).value
                m["context_1m"] = self.query_one(f"#ctx1m-{mid}", Checkbox).value
                raw = self.query_one(f"#efforts-{mid}", Input).value
                m["reasoning_efforts"] = [v.strip() for v in raw.split(",") if v.strip()]
            except Exception:
                pass

    async def _settings_add_model(self):
        self._settings_sync()
        self._sm_models.append({
            "id": "m_" + __import__("uuid").uuid4().hex[:12],
            "api_type": "openai", "provider": "New Model",
            "base_url": "", "api_key": "", "model_name": "",
            "context_1m": False,
            "reasoning_efforts": [],
        })
        await self._settings_render_entries()
        self._settings_refresh_active()

    async def _settings_delete_model(self, mid: str):
        self._settings_sync()
        self._sm_models = [m for m in self._sm_models if m["id"] != mid]
        if self._sm_active == mid:
            self._sm_active = self._sm_models[0]["id"] if self._sm_models else ""
        await self._settings_render_entries()
        self._settings_refresh_active()

    def _settings_save(self):
        self._settings_sync()
        active = self.query_one("#settings-active-model", Select).value
        active = active or (self._sm_models[0]["id"] if self._sm_models else "")
        self._sm_active = active

        config = self.ctrl.config
        new = self._sm_models
        new_ids = {m["id"] for m in new if m.get("id")}
        old_ids = {m["id"] for m in config.list_models()}
        for m in new:
            mid = m.get("id", "")
            if mid in old_ids:
                config.update_model(mid, m)
            elif mid:
                config.add_model(m)
        for mid in old_ids - new_ids:
            config.delete_model(mid)
        config.set_active_model(active)

        self.query_one("#settings-msg", Static).update("Saved!")
        self.set_timer(1, self._close_settings_view)

    # ── send ─────────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _send_message(self, text: str):
        chat = self.query_one(ChatView)
        inp = self.query_one(InputBox)
        inp.clear_input()
        chat.add_user_message(text)
        chat.show_thinking(True)
        inp.set_status("Thinking...")
        streamed = {"text": ""}

        def on_progress(msg: dict):
            if msg.get("type") == "text_delta":
                streamed["text"] += msg.get("content", "")
                chat.stream_agent_text(streamed["text"])

        try:
            result = await self.ctrl.run_prompt(
                text, self._history, on_progress=on_progress,
                on_tool_approval=self._request_tool_approval,
                on_ask_question=self._request_question,
            )
            self._history = result.history
            chat.show_thinking(False)
            final = result.text or streamed["text"]
            chat.add_agent_message(final or "(no output)")
            inp.set_status("Done.")
        except Exception as e:
            chat.show_thinking(False)
            chat.add_error(str(e))
            inp.set_status(f"Error: {e}")
        self._refresh_session_list()

    # ── inline tool approval ───────────────────────────────────────────────

    async def _request_tool_approval(
        self, name: str, params: dict, call_id: str, risk_level: str,
    ) -> bool:
        """Ask the user whether a write/dangerous tool may run (default mode).

        Non-auto-approved tools block the agent loop on a future; the user
        answers in the input box (y/yes allow, anything else denies). The input
        submission handler resolves the future.
        """
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._approval_future = fut
        self._approval_tool_name = name

        chat = self.query_one(ChatView)
        inp = self.query_one(InputBox)
        chat.show_thinking(False)
        inp.clear_input()
        inp.set_status("Awaiting approval...")
        chat.add_info(
            f"[yellow]Approve {name} ({risk_level})?  y/Enter allow, n deny[/]"
        )
        self.query_one("#prompt-input").focus()

        try:
            approved = await fut
        finally:
            self._approval_future = None
            self._approval_tool_name = ""
            inp.set_status("")
        chat.add_info(
            "[green]Approved[/]" if approved else "[red]Denied[/]"
        )
        return approved

    # ── inline question (ask_user_question) ──────────────────────────────

    async def _request_question(self, questions, call_id: str = ""):
        """Collect answers for ask_user_question, one question at a time.

        Mirrors the inline approval flow: each question blocks the agent loop on
        a future, and the input submission handler resolves it. Options are
        answered by number or exact label; option-less questions take free text.
        """
        answers = []
        for q in questions:
            answer = await self._ask_one_question(q)
            answers.append(answer)
        return {"answers": answers}

    async def _ask_one_question(self, q: dict) -> dict:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._question_future = fut

        chat = self.query_one(ChatView)
        inp = self.query_one(InputBox)
        chat.show_thinking(False)
        inp.clear_input()
        inp.set_status("Awaiting your answer...")
        header = q.get("header") or "Question"
        chat.add_info(f"[yellow]{header}[/]  {q['question']}")
        options = q.get("options") or []
        if options:
            for i, opt in enumerate(options, 1):
                desc = f" — {opt['description']}" if opt.get("description") else ""
                chat.add_info(f"  {i}. {opt['label']}{desc}")
            if q.get("multi_select"):
                chat.add_info("[dim]Type numbers separated by commas (e.g. 1,3).[/]")
            else:
                chat.add_info("[dim]Type a number.[/]")
        else:
            chat.add_info("[dim]Type your answer.[/]")
        self.query_one("#prompt-input").focus()

        try:
            raw = await fut
        finally:
            self._question_future = None
            inp.set_status("")
        return self._parse_question_answer(q, raw)

    @staticmethod
    def _parse_question_answer(q: dict, raw: str) -> dict:
        options = q.get("options") or []
        raw = raw.strip()
        if not options:
            return {"id": q["id"], "selected": [], "custom": raw}
        selected: list[str] = []
        for token in raw.replace(",", " ").split():
            try:
                idx = int(token) - 1
                if 0 <= idx < len(options):
                    label = options[idx]["label"]
                else:
                    continue
            except ValueError:
                label = next(
                    (o["label"] for o in options if o["label"].lower() == token.lower()),
                    None,
                )
                if label is None:
                    continue
            if label not in selected:
                selected.append(label)
        return {"id": q["id"], "selected": selected}
