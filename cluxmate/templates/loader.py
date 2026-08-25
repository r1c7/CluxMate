"""Load and render Jinja2 system prompt templates."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


_TEMPLATE_DIR = Path(__file__).parent
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
)


def render_system_prompt(**kwargs) -> str:
    """Render the main system prompt."""
    template = _env.get_template("system_prompt.j2")
    return template.render(**kwargs)


def render_child_prompt(**kwargs) -> str:
    """Render the child subagent system prompt."""
    template = _env.get_template("child_system_prompt.j2")
    return template.render(**kwargs)
