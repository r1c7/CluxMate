"""SkillTool — let the model load and follow an installed skill.

The model calls this with a skill's slug (its directory name, from the
"[Available skills]" injection). The tool returns that skill's
SKILL.md so the model follows it, and signals usage through the builder's
per-turn tracker (same pattern as tools/task.py) so the UI can annotate the
turn with "used skill: X".

The injection lists one row per slug, so a slug installed in both the global
and the project root is loaded from the nearer (project) copy. The result says
which copy served it whenever a copy was shadowed by that rule — the model
sees one row, not two, and must not be silently given a different body than the
one it picked. A skill whose every copy is disabled is refused rather than
loaded (the injection already hides it).
"""

from typing import Any, TYPE_CHECKING

from .base import BaseTool
from cluxmate.core.skills import SkillManager

if TYPE_CHECKING:
    from cluxmate.core.builder import AgentBuilder


class SkillTool(BaseTool):
    """Load an installed skill's instructions and follow them."""

    def __init__(self, cwd: str, builder: "AgentBuilder"):
        self._cwd = cwd
        self._builder = builder

    @property
    def name(self) -> str:
        return "use_skill"

    @property
    def description(self) -> str:
        return (
            "Load an installed skill and follow its instructions. Call this "
            "when a skill from the Available Skills list is relevant to the "
            "task. Returns the skill's full instructions (SKILL.md)."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name/slug from the Available Skills list.",
                },
            },
            "required": ["name"],
        }

    @property
    def risk_level(self) -> str:
        return "safe"

    async def execute(self, name: str = "") -> str:
        mgr = SkillManager(self._cwd)
        skill = mgr.get(name)
        if skill is None:
            available = ", ".join(s.slug for s in mgr.discover_enabled()) or "(none installed)"
            if any(sk.slug == name for sk in mgr.discover()):
                return (
                    f"Error: skill '{name}' is disabled. Available skills: {available}"
                )
            return (
                f"Error: no skill named '{name}'. Available skills: {available}"
            )

        content = mgr.read(skill.slug) or "(SKILL.md was empty or unreadable)"

        # Signal usage through the per-turn tracker, mirroring TaskTool's
        # tracker access. The tracker (set each turn via builder.set_tracker)
        # streams the skill_used event the UI annotates with.
        tracker = getattr(self._builder, "_tracker", None)
        if tracker is not None and hasattr(tracker, "on_skill_used"):
            await tracker.on_skill_used(
                skill.name, skill.slug, skill.source, "auto"
            )

        return (
            f"Skill '{skill.name}' loaded{self._shadow_note(mgr, skill)}. "
            f"Follow these instructions:\n\n{content}"
        )

    @staticmethod
    def _shadow_note(mgr: SkillManager, skill) -> str:
        """Name the copies this one outranks, or "" when nothing was shadowed."""
        losers = mgr.overridden_copies(skill.slug)
        if not losers:
            return ""
        detail = ", ".join(f"{sk.id} ({sk.path})" for sk in losers)
        return f" — the {skill.source} copy takes precedence over {detail}"
