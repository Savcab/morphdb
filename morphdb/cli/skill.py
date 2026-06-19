"""Install the bundled MorphDB Claude skill into a `.claude/skills` directory.

The skill (SKILL.md + the schema CLI) ships as package data inside ``morphdb``,
so this works the same whether MorphDB was pip/brew-installed or run from a
source checkout. It copies the skill tree to ``~/.claude/skills/morphdb`` (or a
project's ``.claude/skills/morphdb``), where Claude Code auto-discovers it.
"""

import os
from importlib import resources

SKILL_NAME = "morphdb"


def _copy_tree(src, dst):
    """Recursively copy an importlib.resources Traversable tree to a real dir."""
    if src.is_dir():
        os.makedirs(dst, exist_ok=True)
        for child in src.iterdir():
            if child.name == "__pycache__":
                continue
            _copy_tree(child, os.path.join(dst, child.name))
    else:
        with open(dst, "wb") as f:
            f.write(src.read_bytes())


def install_skill(claude_dir=None, project=None, force=False):
    """Copy the packaged skill into a `.claude/skills/morphdb` directory.

    ``project`` (a path, or "." for cwd) installs into that project's
    ``.claude``; otherwise it installs into ``~/.claude`` (all projects).
    ``claude_dir`` overrides the `.claude` location outright (used by tests).
    Returns the destination path. Raises FileExistsError if it already exists
    and ``force`` is false.
    """
    if claude_dir is None:
        base = os.path.abspath(project) if project else os.path.expanduser("~")
        claude_dir = os.path.join(base, ".claude")
    dest = os.path.join(claude_dir, "skills", SKILL_NAME)

    if os.path.exists(dest) and not force:
        raise FileExistsError(dest)

    src = resources.files("morphdb") / "skill"
    if not src.is_dir():
        raise FileNotFoundError(
            "packaged skill not found (morphdb/skill missing from the install).")

    if os.path.exists(dest) and force:
        import shutil
        shutil.rmtree(dest)
    _copy_tree(src, dest)
    return dest
