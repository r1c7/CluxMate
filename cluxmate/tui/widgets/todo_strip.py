"""Todo strip — one-line task tracking summary above the input row."""

from textual.widgets import Static


class TodoStrip(Static):
    """Plan strip for the model-declared todo list (todo_write).

    One line: progress counts plus the first in-progress task (with a ``+N``
    suffix when several run in parallel). Hidden while no list is in force —
    the list resets each turn, mirroring the session-log todo/write fold.
    """

    def __init__(self):
        super().__init__("", id="todo-strip")
        self.display = False

    def update_todos(self, todos: list[dict]) -> None:
        if not todos:
            self.display = False
            return
        done = sum(1 for t in todos if t.get("status") == "completed")
        active = [t for t in todos if t.get("status") == "in_progress"]
        pending = sum(1 for t in todos if t.get("status") == "pending")
        parts = [f"[bold]任务[/] {done}/{len(todos)} 完成"]
        if active:
            first = active[0].get("content", "")
            extra = f" +{len(active) - 1}" if len(active) > 1 else ""
            parts.append(f"[yellow]▶[/] {first}{extra}")
        if pending:
            parts.append(f"[dim]{pending} 待处理[/]")
        self.display = True
        self.update(" · ".join(parts))
