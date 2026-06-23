"""``python -m morphdb.cli`` — run the management CLI.

This mirrors the ``morphdb`` console script. It exists so the CLI can re-invoke
itself for detached child processes (e.g. the background dashboard daemon)
without depending on the console script being on ``$PATH``.
"""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
