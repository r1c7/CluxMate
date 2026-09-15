"""SkillManager — discover installed skills for the agent.

A skill is a directory containing a ``SKILL.md``. Two roots are scanned, from
farthest to nearest:
- global:  ``~/.cluxmate/skills/``
- project: ``<cwd>/.cluxmate/skills/``

This mirrors the desktop's read-only browser discovery (main/skills.ts
scanSkillsRoot/parseFrontmatter) but on the Python side, so the agent can
actually surface and follow skills. The identity used for lookup is the
directory name (**slug**) — the frontmatter ``name`` is only a display label
and may contain spaces.

**Name collisions.** The same slug may exist in both roots. The nearer copy
wins (project over global), and that rule lives in exactly one place: only
*enabled* copies are candidates, and among them the nearest one serves the slug
(:meth:`SkillManager.discover_enabled`, :meth:`SkillManager.get`). So every
model-visible surface — the ``[Available skills]`` injection, ``use_skill`` and
its "available skills" fallback text — shows one row per slug, never two rows
with the same key. :meth:`SkillManager.discover` still returns both copies with
``shadowed=True`` on the copy the resolution skipped (an enabled copy only — a
disabled one is merely off), which is what lets the desktop list and toggle
each copy separately.

**Disabling.** Disabled skills are tracked in ``<cwd>/.cluxmate/skills.json`` as
a ``disabledSkills`` array of ids. Two entry forms are accepted:
- a bare ``"deploy"`` disables that slug in **every** root (the original
  format — still what a pre-existing file holds),
- ``"global:deploy"`` / ``"project:deploy"`` disables that single copy, so the
  other copy of a colliding slug keeps serving.
Disabling is applied *before* shadowing, i.e. before picking the winner: a
disabled project copy falls back to its global copy, and a slug becomes
unavailable only when no enabled copy is left. Disabled skills still appear in
:meth:`SkillManager.discover` (so the desktop UI can list + toggle them) but the
``[Available skills]`` injection and SkillTool skip them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_MAX_SKILL_BYTES = 256 * 1024


@dataclass
class Skill:
    slug: str          # directory name — the lookup key (e.g. "deploy")
    name: str          # display label (frontmatter name, or slug)
    description: str
    source: str        # "global" | "project"
    path: str          # absolute path to SKILL.md
    disabled: bool = False
    shadowed: bool = False  # an enabled nearer root serves this slug instead

    @property
    def id(self) -> str:
        """Source-qualified identity — the key ``skills.json`` disables."""
        return f"{self.source}:{self.slug}"


def _parse_frontmatter(md: str) -> dict[str, str]:
    """Extract name/description from a leading ``---`` YAML block.

    Hand-parsed (no YAML dep) — only these two keys, quotes stripped. Anything
    else is ignored. Returns {} when there's no frontmatter.
    """
    if not md.startswith("---"):
        return {}
    end = md.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in md[3:end].split("\n"):
        stripped = line.strip()
        for key in ("name", "description"):
            prefix = f"{key}:"
            if stripped.startswith(prefix):
                v = stripped[len(prefix):].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[key] = v
    return out


class SkillManager:
    """Discover and read skills for a working directory."""

    def __init__(self, cwd: str):
        self._cwd = str(Path(cwd).resolve()) if cwd else str(Path.cwd())

    def _roots(self) -> list[tuple[Path, str]]:
        """Scan roots farthest-first — a later entry shadows an earlier one."""
        return [
            (Path.home() / ".cluxmate" / "skills", "global"),
            (Path(self._cwd) / ".cluxmate" / "skills", "project"),
        ]

    @staticmethod
    def _disabled_by(raw: set[str], source: str, slug: str) -> bool:
        """A bare slug disables every root; ``<source>:<slug>`` disables one copy."""
        return slug in raw or f"{source}:{slug}" in raw

    def _read_disabled_slugs(self) -> set[str]:
        """Raw ids from <cwd>/.cluxmate/skills.json → disabledSkills.

        Entries are kept verbatim (bare slug or ``<source>:<slug>``); which
        copies each one covers is decided by :meth:`_disabled_by`. A file that
        is missing, unreadable, not JSON, or not a JSON *object* simply disables
        nothing — the desktop's twin (main/skills.ts readDisabledSlugs) behaves
        identically, and a hand-edited ``[]``/``null`` must not raise out of
        every turn's injection.
        """
        cfg_path = Path(self._cwd) / ".cluxmate" / "skills.json"
        try:
            cfg = json.loads(cfg_path.read_text("utf-8"))
        except (OSError, ValueError):  # incl. JSONDecodeError, UnicodeDecodeError
            return set()
        if not isinstance(cfg, dict):
            return set()
        items = cfg.get("disabledSkills", [])
        if isinstance(items, list):
            return {s for s in items if isinstance(s, str)}
        return set()

    def discover(self) -> list[Skill]:
        """All skills across both roots, global first then project, A→Z within.

        A slug present in both roots appears twice; the farther copy carries
        ``shadowed=True``. Callers that want the agent-visible set want
        :meth:`discover_enabled` instead.
        """
        raw = self._read_disabled_slugs()
        found: list[Skill] = []
        for root, source in self._roots():
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.is_file():
                    continue
                fm: dict[str, str] = {}
                try:
                    fm = _parse_frontmatter(skill_md.read_text("utf-8")[:4096])
                except OSError:
                    pass
                slug = entry.name
                found.append(Skill(
                    slug=slug,
                    name=fm.get("name") or slug,
                    description=fm.get("description", ""),
                    source=source,
                    path=str(skill_md),
                    disabled=self._disabled_by(raw, source, slug),
                ))
        # A copy is shadowed when a NEARER root provides the same slug and the
        # copy is enabled — i.e. exactly the copies the resolution skipped. A
        # disabled nearer copy shadows nothing (the farther one keeps serving),
        # and a disabled copy isn't shadowed, it's simply off.
        nearest: dict[str, str] = {}
        for sk in found:
            if not sk.disabled:
                nearest[sk.slug] = sk.source
        for sk in found:
            sk.shadowed = (not sk.disabled) and nearest.get(sk.slug) != sk.source
        return found

    def discover_enabled(self) -> list[Skill]:
        """The agent-visible set: one row per slug, sorted by slug.

        Disabled copies are dropped first, then the nearest survivor wins
        (project shadows global). The sort keeps the injected block — and hence
        the request prefix — cache-stable.
        """
        winners: dict[str, Skill] = {}
        for sk in self.discover():
            if sk.disabled:
                continue
            winners[sk.slug] = sk  # _roots() is farthest-first: later wins
        return [winners[slug] for slug in sorted(winners)]

    def get(self, slug: str) -> Skill | None:
        """The enabled copy that serves ``slug`` — the nearest one, else None.

        Search is farthest-first, so the project copy of a colliding slug wins.
        A disabled copy is not a candidate: disabling the project copy falls
        back to its global copy, and disabling every copy makes the slug
        unknown.
        """
        match = None
        for sk in self.discover():
            if sk.slug == slug and not sk.disabled:
                match = sk  # keep scanning; the nearer root wins
        return match

    def overridden_copies(self, slug: str) -> list[Skill]:
        """Enabled copies of ``slug`` that a nearer root overrides.

        What the shadowing rule skipped — surfaced in the ``use_skill`` result
        so the model is not silently handed a different body than the one it
        saw listed. Empty when the loaded copy is the only enabled one.
        """
        return [sk for sk in self.discover() if sk.slug == slug and sk.shadowed]

    def read(self, slug: str) -> str | None:
        """SKILL.md content for a slug, truncated to a sane cap. None if absent."""
        sk = self.get(slug)
        if sk is None:
            return None
        try:
            text = Path(sk.path).read_text("utf-8", errors="replace")
        except OSError:
            return None
        if len(text) > _MAX_SKILL_BYTES:
            text = text[:_MAX_SKILL_BYTES] + "\n\n[skill content truncated]"
        return text
