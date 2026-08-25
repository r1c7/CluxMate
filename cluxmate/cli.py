"""CLI entry point for CluxMate."""

import asyncio
import os
import sys
import time

from cluxmate.core.agent import AgentCallbacks
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.session_log import SessionHeader, SessionLog


class _PrintingCallbacks(AgentCallbacks):
    """Streams the model's text to stdout as it arrives."""

    def __init__(self):
        self.saw_text = False

    async def on_text_delta(self, chunk: str) -> None:
        self.saw_text = True
        print(chunk, end="", flush=True)

    async def ask_question(self, questions, call_id: str = ""):
        """Collect answers interactively on stdin (headless / REPL).

        ``input()`` blocks, so each prompt runs on an executor thread while the
        agent loop's event loop stays free. Options are answered by number (or
        exact label); option-less questions take free text.
        """
        loop = asyncio.get_running_loop()
        answers = []
        for q in questions:
            header = q.get("header") or "Question"
            print(f"\n[{header}] {q['question']}")
            options = q.get("options") or []
            if options:
                for i, opt in enumerate(options, 1):
                    desc = f" — {opt['description']}" if opt.get("description") else ""
                    print(f"    {i}. {opt['label']}{desc}")
                suffix = " (comma-separated numbers)" if q.get("multi_select") else ""
                raw = await loop.run_in_executor(
                    None, input, f"  Your choice{suffix}: "
                )
                selected = _parse_option_answer(raw, options)
                answers.append({"id": q["id"], "selected": selected})
            else:
                raw = await loop.run_in_executor(None, input, "  Your answer: ")
                answers.append({"id": q["id"], "selected": [], "custom": raw.strip()})
        return {"answers": answers}


def _parse_option_answer(raw: str, options: list[dict]) -> list[str]:
    """Parse a comma/space-separated answer into option labels (numbers or text)."""
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
    return selected


def _resolve_entry(model_id: str | None):
    """Resolve a config model entry by id, falling back to the active model."""
    from cluxmate.core.config import ConfigManager
    config = ConfigManager()
    entry = config.get_model(model_id) if model_id else None
    if entry is None:
        entry = config.get_active_model()
    if entry is None:
        print("Error: no model configured. Run `cluxmate` and open Settings, "
              "or edit ~/.cluxmate/config.json.")
        sys.exit(1)
    return entry


def _make_log(entry: dict) -> SessionLog:
    return SessionLog.create(SessionHeader(
        id="cli", createdAt=int(time.time() * 1000),
        provider=entry.get("provider", ""), model=entry.get("model_name", ""),
        apiType=entry.get("api_type", ""),
    ))


async def run_headless(
    prompt: str,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Run a single prompt in headless mode, print result."""
    cwd = os.getcwd()
    entry = _resolve_entry(model_id)
    from cluxmate.core.providers.factory import build_provider
    from cluxmate.core.reasoning import coerce_effort, default_for
    provider = build_provider(entry)
    # An explicit --reasoning-effort wins ("default"/"" = don't send); otherwise
    # the model's preset default.
    eff = coerce_effort(reasoning_effort) if reasoning_effort is not None else default_for(entry)
    provider.set_reasoning_effort(eff)

    builder = AgentBuilder(cwd, provider)
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.with_model(entry.get("model_name", ""))
    builder.with_context_1m(entry.get("context_1m", False))

    agent = builder.build(session_log=_make_log(entry))
    cbs = _PrintingCallbacks()
    result = await agent.run(
        prompt, callbacks=cbs, injections=builder.injections_for_turn(),
    )

    if cbs.saw_text:
        print()  # terminate the streamed line
    else:
        print(result.text or "(no output)")


async def run_repl(model_id: str | None = None, reasoning_effort: str | None = None) -> None:
    """Interactive REPL mode."""
    cwd = os.getcwd()
    entry = _resolve_entry(model_id)
    from cluxmate.core.providers.factory import build_provider
    from cluxmate.core.reasoning import coerce_effort, default_for
    provider = build_provider(entry)
    eff = coerce_effort(reasoning_effort) if reasoning_effort is not None else default_for(entry)
    provider.set_reasoning_effort(eff)

    builder = AgentBuilder(cwd, provider)
    builder.with_default_tools()
    builder.with_subagent_types(["general-purpose", "explore"])
    builder.with_model(entry.get("model_name", ""))
    builder.with_context_1m(entry.get("context_1m", False))

    log = _make_log(entry)
    agent = builder.build(session_log=log)

    print("CluxMate REPL. Type /exit to quit, /clear to reset history.")
    print(f"Model: {entry.get('provider', '')} / {entry.get('model_name', '')}")
    if eff:
        print(f"Reasoning effort: {eff}")
    print(f"Working directory: {cwd}")
    print()

    history = []
    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not user_input:
            continue

        if user_input == "/exit":
            print("Goodbye.")
            break

        if user_input == "/clear":
            history = []
            log = _make_log(entry)
            agent.session_log = log
            print("[History cleared]")
            continue

        print()
        cbs = _PrintingCallbacks()
        result = await agent.run(
            user_input, history, callbacks=cbs,
            injections=builder.injections_for_turn(),
        )
        if agent.compacted_this_turn:
            builder.invalidate_injections()
        if not cbs.saw_text:
            print(result.text or "(no output)")
        print()
        history = result.history


async def run_tui() -> None:
    """Launch the Textual TUI."""
    from cluxmate.tui.app import CluxMateApp

    app = CluxMateApp()
    await app.run_async()


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="cluxmate",
        description="CluxMate — a terminal AI coding agent",
    )
    sub = parser.add_subparsers(dest="command")

    # cluxmate agent stdio
    agent_parser = sub.add_parser("agent", help="Agent daemon mode")
    agent_parser.add_argument(
        "mode", choices=["stdio", "serve"], default="stdio",
        help="Communication mode",
    )

    # cluxmate repl
    repl_parser = sub.add_parser("repl", help="Interactive REPL mode")
    repl_parser.add_argument(
        "--model-id", dest="model_id", default=argparse.SUPPRESS,
        help="Config model entry id to use (defaults to the active model).",
    )
    repl_parser.add_argument(
        "--reasoning-effort", dest="reasoning_effort", default=argparse.SUPPRESS,
        help="Reasoning level id to use (e.g. high/max/off; defaults to the provider default).",
    )

    # cluxmate -p "..."
    parser.add_argument("-p", "--prompt", help="Run in headless mode with the given prompt.")
    parser.add_argument("--model-id", dest="model_id", help="Config model entry id to use (defaults to the active model).")
    parser.add_argument("--reasoning-effort", dest="reasoning_effort", help="Reasoning level id to use (e.g. high/max/off; defaults to the provider default).")

    args = parser.parse_args()

    if args.command == "agent":
        if args.mode == "stdio":
            from cluxmate.core.jsonrpc_server import main as run_stdio
            run_stdio()
        return

    if args.command == "repl":
        asyncio.run(run_repl(args.model_id, args.reasoning_effort))
        return

    if args.prompt:
        asyncio.run(run_headless(args.prompt, args.model_id, args.reasoning_effort))
    else:
        asyncio.run(run_tui())


if __name__ == "__main__":
    main()
