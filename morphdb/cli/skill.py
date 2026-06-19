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


def install_skill(claude_dir=None, project=None):
    """Copy the packaged skill into a `.claude/skills/morphdb` directory.

    Idempotent: re-running overwrites the destination with the skill bundled in
    the *currently installed* ``morphdb`` package, so it always lands the version
    you have (run ``pip install -U morphdb`` first to refresh to the latest). It
    reads from package data, not from GitHub.

    ``project`` (a path, or "." for cwd) installs into that project's
    ``.claude``; otherwise it installs into ``~/.claude`` (all projects).
    ``claude_dir`` overrides the `.claude` location outright (used by tests).
    Returns ``(dest_path, existed_before)``.
    """
    if claude_dir is None:
        base = os.path.abspath(project) if project else os.path.expanduser("~")
        claude_dir = os.path.join(base, ".claude")
    dest = os.path.join(claude_dir, "skills", SKILL_NAME)

    src = resources.files("morphdb") / "skill"
    if not src.is_dir():
        raise FileNotFoundError(
            "packaged skill not found (morphdb/skill missing from the install).")

    existed = os.path.exists(dest)
    if existed:
        import shutil
        shutil.rmtree(dest)
    _copy_tree(src, dest)
    return dest, existed
