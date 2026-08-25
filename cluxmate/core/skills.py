"""SkillManager — discover installed skills for the agent.

A skill is a directory containing a ``SKILL.md``. Two roots are scanned:
- global:  ``~/.cluxmate/skills/``
- project: ``<cwd>/.cluxmate/skills/``

This mirrors the desktop's read-only browser discovery (ipc-handlers.ts
scanSkillsRoot/parseFrontmatter) but on the Python side, so the agent can
actually surface and follow skills. The identity used for lookup is the
directory name (**slug**) — the frontmatter ``name`` is only a display label
and may contain spaces.

Disabled skills are tracked in ``<cwd>/.cluxmate/skills.json`` as a
``disabledSkills`` array of slugs. They still appear in discovery (so the
desktop UI can list + toggle them) but the ``[Available skills]`` injection
and SkillTool skip them.
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
        return [
            (Path.home() / ".cluxmate" / "skills", "global"),
            (Path(self._cwd) / ".cluxmate" / "skills", "project"),
        ]

    def _read_disabled_slugs(self) -> set[str]:
        """Slugs listed in <cwd>/.cluxmate/skills.json → disabledSkills."""
        cfg_path = Path(self._cwd) / ".cluxmate" / "skills.json"
        try:
            cfg = json.loads(cfg_path.read_text("utf-8"))
            items = cfg.get("disabledSkills", [])
            if isinstance(items, list):
                return {s for s in items if isinstance(s, str)}
        except (OSError, json.JSONDecodeError):
            pass
        return set()

    def discover(self) -> list[Skill]:
        """All skills across both roots, global first then project, A→Z within."""
        disabled = self._read_disabled_slugs()
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
                    disabled=slug in disabled,
                ))
        return found

    def discover_enabled(self) -> list[Skill]:
        """Skills that are NOT disabled — what the agent actually sees."""
        return [sk for sk in self.discover() if not sk.disabled]

    def get(self, slug: str) -> Skill | None:
        """Find a skill by directory-name slug (project shadows global)."""
        match = None
        for sk in self.discover():
            if sk.slug == slug:
                match = sk  # keep scanning; project (listed later) wins
        return match

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
