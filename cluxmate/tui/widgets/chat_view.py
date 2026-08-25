"""Chat scrollback widget — displays conversation messages."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, RichLog


class ChatView(Vertical):
    """Main area: displays user/agent/tool messages."""

    def compose(self) -> ComposeResult:
        yield RichLog(id="chat-log", markup=True, wrap=True, highlight=True)
        yield Static("", id="thinking-indicator")

    def _write(self, prefix: str, prefix_style: str, text: str):
        self.query_one(RichLog).write(Text(prefix, style=prefix_style))
        self.query_one(RichLog).write(Text(text + "\n"))

    def add_user_message(self, text: str):
        self._write("You: ", "bold blue", text)

    def add_agent_message(self, text: str):
        self._write("Agent: ", "bold green", text)

    def add_tool_start(self, name: str):
        self.query_one(RichLog).write(Text(f"{name}\n", style="yellow"))

    def add_tool_result(self, text: str):
        preview = text[:200].replace("\n", " ")
        self.query_one(RichLog).write(Text(f"  ↳ {preview}\n", style="dim"))

    def add_info(self, text: str):
        self.query_one(RichLog).write(Text(text + "\n", style="dim italic"))

    def add_error(self, text: str):
        self.query_one(RichLog).write(Text(f"Error: {text}\n", style="red"))

    def show_thinking(self, visible: bool):
        indicator = self.query_one("#thinking-indicator", Static)
        indicator.display = visible
        indicator.update("[dim italic]Thinking...[/]" if visible else "")

    def stream_agent_text(self, text: str):
        indicator = self.query_one("#thinking-indicator", Static)
        indicator.display = True
        indicator.update(Text(text))

    def clear(self):
        self.query_one(RichLog).clear()
