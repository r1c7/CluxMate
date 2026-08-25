"""Prompt input widget — bottom input area."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Input, Static


class InputBox(Container):
    """Bottom bar: prompt input + status line."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="input-row"):
            yield Input(
                placeholder=(
                    "Ask anything... (Enter send)  "
                    "Ctrl+N new  Ctrl+D delete  Ctrl+S settings  Ctrl+O dir  Ctrl+M mode"
                ),
                id="prompt-input",
            )
        yield Static("", id="status-bar")

    def on_mount(self):
        self.query_one("#prompt-input", Input).focus()

    def set_status(self, text: str):
        self.query_one("#status-bar", Static).update(text)

    def clear_input(self):
        inp = self.query_one("#prompt-input", Input)
        inp.clear()
