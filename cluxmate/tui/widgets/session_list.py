"""Session list widget — sidebar showing saved sessions."""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, Button, Label


class SessionList(Vertical):
    """Left sidebar: list of saved sessions."""

    def compose(self) -> ComposeResult:
        with Static(classes="section-header"):
            yield Label("Sessions")
        with VerticalScroll(id="session-items"):
            yield Static("No sessions", id="session-empty", classes="dim")
        with Vertical(id="session-actions"):
            yield Button("+ New", id="btn-new-session", variant="primary")
            yield Button("Delete", id="btn-delete-session", variant="error")
            yield Button("Settings", id="btn-open-settings")

    async def update_list(self, sessions: list[dict], active_id: str | None):
        container = self.query_one("#session-items", VerticalScroll)
        # remove_children() is async — it posts Prune messages and only
        # detaches widgets from the node list later on the message pump.
        # Not awaiting it makes re-mounting a session button whose old
        # instance is still attached raise DuplicateIds.
        await container.remove_children()

        valid = [s for s in sessions if s.get("id") and s.get("provider")]
        if not valid:
            await container.mount(
                Static("No sessions", id="session-empty", classes="dim"),
            )
            return
        buttons = []
        for i, s in enumerate(valid):
            sid = s["id"]
            label = s.get("title", "Untitled")[:30]
            if sid == active_id:
                label = f"> {label}"
            btn = Button(label, id=f"session-btn-{sid}", classes="session-btn")
            btn.tooltip = (
                f"{s.get('provider','?')} / {s.get('model','?')}"
                f" — {s.get('updated_at','')[:10]}"
            )
            buttons.append(btn)
        await container.mount_all(buttons)
