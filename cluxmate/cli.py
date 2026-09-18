"""CLI entry point for CluxMate."""

import asyncio
import json
import os
import sys
import time

from cluxmate.core import project_root
from cluxmate.core.agent import AgentCallbacks
from cluxmate.core.builder import AgentBuilder
from cluxmate.core.session_log import SessionHeader, SessionLog
from cluxmate.core.trust import DENIED, TRUSTED


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

    builder = AgentBuilder(cwd, provider, project_root=_config_root(cwd))
    builder.with_default_tools()
    builder.with_subagents()
    builder.with_model(entry.get("model_name", ""))
    builder.with_context_1m(entry.get("context_1m", False))
    decision = _trust_decision(cwd)
    _warn_untrusted(decision)
    builder.with_trust(decision)

    agent = builder.build(session_log=_make_log(entry))
    hooks = builder._hooks_manager()
    injections = builder.injections_for_turn()

    # SessionStart hook: fires once at startup. A block aborts the run before
    # the model sees anything; feedback is prepended to the first turn.
    if hooks.has_event("SessionStart"):
        hr = await hooks.run_event("SessionStart", extra={"source": "startup"})
        if hr.blocked:
            print(hr.reason or "[SessionStart hook blocked the session]")
            return
        injections = [("hook", fb) for fb in hr.feedback] + injections

    cbs = _PrintingCallbacks()
    try:
        result = await agent.run(
            prompt, callbacks=cbs, injections=injections,
        )
    finally:
        # SessionEnd fires at process exit — even when the run raised. Output
        # is discarded (nothing left to block or feed).
        if hooks.has_event("SessionEnd"):
            await hooks.run_event("SessionEnd", extra={"reason": "exit"})

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

    builder = AgentBuilder(cwd, provider, project_root=_config_root(cwd))
    builder.with_default_tools()
    builder.with_subagents()
    builder.with_model(entry.get("model_name", ""))
    builder.with_context_1m(entry.get("context_1m", False))
    decision = _trust_decision(cwd)
    _prompt_trust(decision)
    # The answer just landed in the store, but `decision` is a frozen snapshot
    # taken before the prompt — re-resolve so the run that grants trust is the
    # run that gets it (and so the banner below reports what was actually used).
    decision = _trust_decision(cwd)
    builder.with_trust(decision)

    log = _make_log(entry)
    agent = builder.build(session_log=log)
    hooks = builder._hooks_manager()
    # SessionStart feedback applies to the FIRST turn only (cleared on use).
    session_feedback: list[str] = []
    if hooks.has_event("SessionStart"):
        hr = await hooks.run_event("SessionStart", extra={"source": "startup"})
        if hr.blocked:
            print(hr.reason or "[SessionStart hook blocked the session]")
            return
        session_feedback = hr.feedback

    print("CluxMate REPL. Type /exit to quit, /clear to reset history.")
    print(f"Model: {entry.get('provider', '')} / {entry.get('model_name', '')}")
    if eff:
        print(f"Reasoning effort: {eff}")
    print(f"Working directory: {cwd}")
    if decision.gated:
        print("Project config: NOT loaded (directory not trusted)")
    print()

    history = []
    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            if hooks.has_event("SessionEnd"):
                await hooks.run_event("SessionEnd", extra={"reason": "exit"})
            print("\nGoodbye.")
            break

        if not user_input:
            continue

        if user_input == "/exit":
            if hooks.has_event("SessionEnd"):
                await hooks.run_event("SessionEnd", extra={"reason": "exit"})
            print("Goodbye.")
            break

        if user_input == "/clear":
            # /clear ends the old session and starts a new one (both events
            # fire with source/reason "clear"). A blocking SessionStart here
            # only prints the reason — the REPL keeps running.
            if hooks.has_event("SessionEnd"):
                await hooks.run_event("SessionEnd", extra={"reason": "clear"})
            history = []
            log = _make_log(entry)
            agent.session_log = log
            session_feedback = []
            if hooks.has_event("SessionStart"):
                hr = await hooks.run_event("SessionStart", extra={"source": "clear"})
                if hr.blocked:
                    print(hr.reason or "[SessionStart hook blocked the session]")
                else:
                    session_feedback = hr.feedback
            print("[History cleared]")
            continue

        print()
        injections = builder.injections_for_turn()
        if session_feedback:
            injections = [("hook", fb) for fb in session_feedback] + injections
            session_feedback = []
        cbs = _PrintingCallbacks()
        result = await agent.run(
            user_input, history, callbacks=cbs, injections=injections,
        )
        if agent.compacted_this_turn:
            builder.invalidate_injections()
        if not cbs.saw_text:
            print(result.text or "(no output)")
        print()
        history = result.history


def _mcp_withheld(decision) -> bool:
    """Whether the trust gate kept this directory's mcp.json out of `mcp`."""
    return not decision.trusted and any(f.kind == "mcp" for f in decision.findings)


def _mcp_withheld_notice(decision) -> None:
    """Name the project mcp.json `mcp` is withholding, on stderr.

    Only when the directory is untrusted AND it actually ships one — otherwise
    the notice is noise. The exit code is untouched: a missing server is still
    "unknown server" to a caller, and stdout stays machine-readable.
    """
    if not _mcp_withheld(decision):
        return
    withheld = os.path.join(decision.cwd, ".cluxmate", "mcp.json")
    print(
        f"note: not loading {withheld} — {decision.cwd} is not trusted; "
        f"run `cluxmate trust add` in it to enable its MCP servers",
        file=sys.stderr,
    )


def run_mcp(args) -> int:
    """`cluxmate mcp auth|logout|status` — the OAuth entry point.

    Reads mcp.json directly (MCPConfigManager), so it needs neither a running
    session nor a live bridge: authorization is a user-level credential
    operation. Credentials land in ~/.cluxmate/mcp-auth.json and take effect in
    the next session for the CLI; the desktop hot-swaps them instead.

    The read is trust-gated like every other project-config reader: an untrusted
    repository must not be able to list or authenticate its own MCP servers —
    `auth` would otherwise probe a URL that repository supplied.
    """
    import json as _json
    import urllib.parse

    from cluxmate.core.mcp import MCPConfigManager
    from cluxmate.core.mcp_auth_store import MCPAuthStore
    from cluxmate.core.mcp_oauth import MCPOAuthFlow, OAuthError

    if getattr(args, "mcp_command", None) is None:
        # `cluxmate mcp` with no subcommand: say so instead of crashing in the
        # auth branch on the missing `name`.
        print("error: specify a subcommand: auth, logout or status", file=sys.stderr)
        return 1

    # `--cwd` names a PROJECT: inside a linked worktree the project's config is
    # the main worktree's, so every read (and every message) below is about the
    # config root. A non-worktree directory is its own config root — unchanged.
    config_root = _config_root(getattr(args, "cwd", None) or os.getcwd())
    decision = _trust_decision(config_root)
    _mcp_withheld_notice(decision)
    configs = MCPConfigManager(config_root, trusted=decision.trusted).load()
    store = MCPAuthStore()

    if args.mcp_command == "logout":
        removed = store.delete(args.name)
        print(f"{'removed' if removed else 'no credentials for'} {args.name}")
        return 0

    if args.mcp_command == "status":
        rows = []
        for name, cfg in configs.items():
            described = store.describe(name, cfg.url or "") if cfg.url else None
            rows.append({
                "name": name,
                "transport": "local" if cfg.transport == "stdio" else "remote",
                "oauth": cfg.oauth is not None,
                "authenticated": bool(described),
                "expires_at": (described or {}).get("expires_at"),
                "error": cfg.oauth_error,
            })
        if getattr(args, "json", False):
            print(_json.dumps(rows, indent=2))
        else:
            for r in rows:
                state = "authorized" if r["authenticated"] else (
                    "oauth (not authorized)" if r["oauth"] else "-")
                print(f"{r['name']:<24} {r['transport']:<7} {state}")
        return 0

    cfg = configs.get(args.name)
    if cfg is None:
        if _mcp_withheld(decision):
            # To the user staring at their own mcp.json this is indistinguishable
            # from a typo, so name the gate as the cause and the way out.
            print(
                f"error: unknown MCP server {args.name!r} — its project "
                f".cluxmate/mcp.json was not loaded because {decision.cwd} is "
                f"not trusted; run `cluxmate trust add` in it to enable its MCP "
                f"servers",
                file=sys.stderr,
            )
        else:
            print(f"error: unknown MCP server {args.name!r}", file=sys.stderr)
        return 1
    if cfg.transport != "http":
        print(f"error: server {args.name!r} is local (stdio) — OAuth does not apply",
              file=sys.stderr)
        return 1
    if cfg.oauth is None:
        print(f"error: server {args.name!r} has OAuth disabled in mcp.json",
              file=sys.stderr)
        return 1
    if args.callback_port is not None:
        cfg.oauth.callback_port = int(args.callback_port)

    open_browser = not getattr(args, "no_browser", False)
    timeout = float(getattr(args, "timeout", 300.0) or 300.0)

    def _print_authorize_url(url: str) -> None:
        """`--no-browser`: hand over the URL and how to reach the callback.

        The listener sits on THIS machine's loopback interface, so a browser
        running anywhere else (SSH / headless) cannot reach it without a port
        forward. There is deliberately no "paste the callback URL back" path:
        the callback must be reachable over HTTP by the browser itself.
        """
        print(f"open this URL in a browser:\n  {url}\n")
        # The port that is really listening is the one the flow put in the
        # authorization request's redirect_uri: with the default ephemeral port
        # cfg.callback_port is 0, so reading it from the flow would print a
        # useless ":0" (flow.callback_redirect() does not know the bound port
        # unless the caller passes it).
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        redirect = (query.get("redirect_uri") or [""])[0] or (
            f"http://127.0.0.1:{cfg.oauth.callback_port}/oauth/callback")
        port = urllib.parse.urlsplit(redirect).port
        print(f"the browser must reach this machine's loopback callback {redirect}")
        print("— on an SSH / headless session, forward that port first:")
        print(f"  ssh -L {port}:127.0.0.1:{port} <user>@<host>\n")

    flow = MCPOAuthFlow(
        cfg.oauth, open_browser=open_browser, callback_timeout=timeout,
        on_authorize_url=None if open_browser else _print_authorize_url,
    )
    try:
        record = flow.authorize(flow.probe())
    except OAuthError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.kind == "timeout" and not open_browser:
            print("hint: that callback is on this machine's loopback interface — "
                  "an SSH user must forward it first, e.g. "
                  "`ssh -L <port>:127.0.0.1:<port> <user>@<host>`",
                  file=sys.stderr)
        return 1
    except OSError as e:
        # authorize() already wraps a loopback bind failure in an OAuthError,
        # but any other OSError escaping the flow must still exit 1 with a
        # message instead of a traceback.
        print(f"error: MCP OAuth flow failed: {e}", file=sys.stderr)
        return 1
    store.put(args.name, record)
    scope = f" (scope: {record.scope})" if record.scope else ""
    print(f"authorized {args.name}{scope} — takes effect in a new session")
    return 0


# ── project trust ─────────────────────────────────────────────────────────

# One store per process (the registry is a file; the overrides are per-run).
_TRUST_STORE = None


def _trust_store():
    global _TRUST_STORE
    if _TRUST_STORE is None:
        from cluxmate.core.trust import TrustStore

        _TRUST_STORE = TrustStore()
    return _TRUST_STORE


def _config_root(cwd: str) -> str:
    """The directory this project's CONFIG state is read from and keyed on.

    The session's own directory everywhere except inside a LINKED git worktree,
    where it is the main worktree (see cluxmate/core/project_root.py). Trust,
    permissions and every project config file take this; the session's cwd stays
    the tree the builder is handed as the writable/exec tree. The same rule the
    JSON-RPC server applies once in `initialize` — the front ends must agree, or
    a worktree session reads a different project than the desktop does.
    """
    return project_root.resolve(cwd).config_root


def _trust_decision(cwd: str):
    from cluxmate.core.trust import resolve_trust

    # The answer is about the PROJECT, so it is resolved on the config root: a
    # worktree is not a project of its own, a plain-repo subdirectory is.
    return resolve_trust(_config_root(cwd), _trust_store())


def _warn_untrusted(decision) -> None:
    """Headless/REPL notice — there is no prompt outside a TTY.

    Names ``decision.cwd`` — the config root the decision was resolved on, i.e.
    the directory whose `.cluxmate/` is being withheld (a worktree session's own
    path would name a directory nothing is read from).
    """
    if not decision.gated:
        return
    kinds = ", ".join(f.kind for f in decision.findings)
    print(
        f"warning: {decision.cwd} is not trusted — project config ({kinds}) was "
        f"NOT loaded; run `cluxmate trust add` in it to enable hooks/MCP/etc.",
        file=sys.stderr,
    )


def _prompt_trust(decision) -> None:
    """Ask once, interactively (REPL only). No TTY ⇒ leave the registry alone."""
    if not decision.pending or not sys.stdin.isatty():
        _warn_untrusted(decision)
        return
    # `decision.cwd` is the canonical config root this answer resolves on —
    # asking about it and recording the answer under it keeps this write and the
    # next read (a real session's lookup) on the same key.
    print(f"\n{decision.cwd} ships project config under .cluxmate/:")
    for f in decision.findings:
        print(f"  - {f.label}  ({f.path})")
    print("Loading it lets this repository run hooks / MCP servers and pre-authorize tools.")
    while True:
        try:
            answer = input(
                "Trust this directory? [1] yes, remember  [2] yes, this run only  "
                "[3] no: "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if answer == "1":
            _trust_store().set(decision.cwd, TRUSTED)
            return
        if answer == "2":
            _trust_store().set_session(decision.cwd, TRUSTED)
            return
        if answer == "3":
            _trust_store().set(decision.cwd, DENIED)
            return


def run_trust(args) -> int:
    """`cluxmate trust [list|add|deny|remove|status] [path]`."""
    path = os.path.abspath(args.path) if getattr(args, "path", None) else os.getcwd()
    # A worktree is not a project of its own: record (and report) the answer at
    # the config root, where sessions and the desktop read it — otherwise
    # `trust add` inside a worktree would file an entry nobody ever reads.
    path = _config_root(path)
    action = getattr(args, "action", "list") or "list"
    store = _trust_store()
    if action == "list":
        entries = store.entries()
        if not entries:
            print("No trust decisions recorded.")
            return 0
        for entry_path, status in sorted(entries.items()):
            print(f"{status:8} {entry_path}")
        return 0
    if action == "add":
        store.set(path, TRUSTED)
        print(f"trusted: {path}")
        return 0
    if action == "deny":
        store.set(path, DENIED)
        print(f"denied: {path}")
        return 0
    if action == "remove":
        print(f"removed: {path}" if store.remove(path)
              else f"no decision recorded for {path}")
        return 0
    decision = _trust_decision(path)
    print(f"status: {decision.status} ({decision.source})")
    if decision.findings:
        print("project config found:")
        for f in decision.findings:
            print(f"  {f.kind:11} {f.path}")
    else:
        print("no project config found — nothing to gate")
    return 0


def run_worktree(args) -> int:
    """`cluxmate worktree [info|create|list|remove] [--cwd DIR] [--json]`.

    The CLI is a thin printer over `cluxmate.core.worktree`: the core resolves,
    validates and runs git, and hands back either the `ok: true` payload or a
    `WorktreeError`. In `--json` mode stdout carries EXACTLY one JSON line for
    both outcomes (the desktop parses stdout, not stderr) and a failure exits 1;
    without it the payload is rendered for a human and a failure prints
    `error: <message>` on stderr.
    """
    from cluxmate.core import worktree

    cwd = os.path.abspath(args.cwd) if getattr(args, "cwd", None) else os.getcwd()
    action = getattr(args, "worktree_command", None) or "info"
    as_json = bool(getattr(args, "json", False))
    try:
        if action == "create":
            payload = worktree.create(
                cwd,
                name=getattr(args, "name", None),
                title=getattr(args, "title", None),
                base=getattr(args, "base", None),
                branch=getattr(args, "branch", None),
                allow_dirty=bool(getattr(args, "allow_dirty", False)),
            )
        elif action == "list":
            payload = {"ok": True, "worktrees": worktree.list_worktrees(cwd)}
        elif action == "remove":
            payload = worktree.remove(
                cwd,
                args.target,
                force=bool(getattr(args, "force", False)),
                keep_branch=bool(getattr(args, "keep_branch", False)),
            )
        else:
            payload = worktree.info(cwd)
    except worktree.WorktreeError as exc:
        if as_json:
            print(json.dumps({
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "details": exc.details,
            }))
        else:
            print(f"error: {exc.message}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(payload))
        return 0
    _print_worktree(action, payload)
    return 0


def _print_worktree(action: str, payload: dict) -> None:
    """Human-readable rendering of the same payloads `--json` prints."""
    if action == "info":
        branch = payload["branch"] or "-"
        print(f"cwd:         {payload['cwd']}")
        print(f"root:        {payload['root']}")
        print(f"config_root: {payload['config_root']}")
        print(f"worktree:    {'yes' if payload['is_worktree'] else 'no'}")
        print(f"branch:      {branch}")
        return
    if action == "create":
        print(f"created {payload['path']}")
        print(f"  name:   {payload['name']}")
        print(f"  branch: {payload['branch']} "
              f"(from {payload['base_ref']} {payload['base'][:12]})")
        if payload["dirty"]:
            print(f"  note:   the main worktree was dirty "
                  f"({len(payload['dirty'])} file(s), --allow-dirty)")
        return
    if action == "list":
        for row in payload["worktrees"]:
            mark = "*" if row["is_current"] else " "
            label = row["name"] or ("(main)" if row["is_main"] else "-")
            branch = row["branch"] or "-"
            print(f"{mark} {label:24} {branch:32} {row['path']}")
        return
    print(f"removed {payload['path']}")
    print(f"  branch: {payload['branch'] or '-'} "
          f"({'deleted' if payload['branch_deleted'] else 'kept'})")
    if payload["pruned"]:
        print("  note:   the directory was already gone; "
              "`git worktree prune` cleaned the entry")
    if payload.get("message"):
        print(f"  note:   {payload['message']}")


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

    # cluxmate mcp auth|logout|status
    mcp_parser = sub.add_parser("mcp", help="Remote MCP server OAuth")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_auth = mcp_sub.add_parser("auth", help="Authorize a remote MCP server")
    mcp_auth.add_argument("name")
    mcp_auth.add_argument("--cwd", default=None, help="Project directory (defaults to cwd)")
    mcp_auth.add_argument("--callback-port", dest="callback_port", type=int, default=None,
                          help="Fixed loopback port (default: an ephemeral one, registered with the AS)")
    mcp_auth.add_argument("--no-browser", dest="no_browser", action="store_true",
                          help="Print the authorization URL instead of opening a browser")
    mcp_auth.add_argument("--timeout", type=float, default=300.0,
                          help="Seconds to wait for the callback (default 300)")
    mcp_logout = mcp_sub.add_parser("logout", help="Delete stored credentials")
    mcp_logout.add_argument("name")
    mcp_logout.add_argument("--cwd", default=None)
    mcp_status = mcp_sub.add_parser("status", help="Show configured servers and auth state")
    mcp_status.add_argument("--cwd", default=None)
    mcp_status.add_argument("--json", action="store_true")

    # cluxmate trust list|add|deny|remove|status [path]
    trust_parser = sub.add_parser("trust", help="Project trust for <dir>/.cluxmate config")
    trust_parser.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "add", "deny", "remove", "status"],
    )
    trust_parser.add_argument("path", nargs="?", default=None)

    # cluxmate worktree info|create|list|remove
    wt_parser = sub.add_parser(
        "worktree", help="Create / remove the linked worktrees sessions run in"
    )
    wt_sub = wt_parser.add_subparsers(dest="worktree_command")
    wt_info = wt_sub.add_parser("info", help="Resolve the project root of a directory")
    wt_info.add_argument("--cwd", default=None, help="Project directory (defaults to cwd)")
    wt_info.add_argument("--json", action="store_true", help="Print one line of JSON")

    wt_create = wt_sub.add_parser("create", help="Create a linked worktree")
    wt_create.add_argument("--cwd", default=None, help="Project directory (defaults to cwd)")
    wt_create.add_argument("--json", action="store_true", help="Print one line of JSON")
    wt_create.add_argument("--name", default=None,
                           help="Worktree name (default: derived from --title, else wt)")
    wt_create.add_argument("--title", default=None,
                           help="Session title the worktree name is derived from")
    wt_create.add_argument("--base", default=None,
                           help="Commit or ref to base the worktree on (default: HEAD)")
    wt_create.add_argument("--branch", default=None,
                           help="Branch name (default: cluxmate/<name>)")
    wt_create.add_argument("--allow-dirty", dest="allow_dirty", action="store_true",
                           help="Create even when the main worktree has uncommitted changes")

    wt_list = wt_sub.add_parser("list", help="List the repository's worktrees")
    wt_list.add_argument("--cwd", default=None, help="Project directory (defaults to cwd)")
    wt_list.add_argument("--json", action="store_true", help="Print one line of JSON")

    wt_remove = wt_sub.add_parser("remove", help="Remove a worktree and its branch")
    wt_remove.add_argument("target", help="Worktree name, or a path under <repo>/.worktrees")
    wt_remove.add_argument("--cwd", default=None, help="Project directory (defaults to cwd)")
    wt_remove.add_argument("--json", action="store_true", help="Print one line of JSON")
    wt_remove.add_argument("--force", action="store_true",
                           help="Discard uncommitted changes in the worktree")
    wt_remove.add_argument("--keep-branch", dest="keep_branch", action="store_true",
                           help="Keep the branch after removing the tree")

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

    if args.command == "mcp":
        sys.exit(run_mcp(args))

    if args.command == "trust":
        sys.exit(run_trust(args))

    if args.command == "worktree":
        sys.exit(run_worktree(args))

    if args.prompt:
        asyncio.run(run_headless(args.prompt, args.model_id, args.reasoning_effort))
    else:
        asyncio.run(run_tui())


if __name__ == "__main__":
    main()
