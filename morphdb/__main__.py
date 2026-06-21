"""CLI entry point: ``python -m morphdb`` or the ``morphdb`` console script."""

import argparse
import sys

from . import __version__
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="morphdb",
        description="MorphDB — a coding-agent-friendly, multi-tenant backend for vibe-coded websites.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host/interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787,
                        help="Port to listen on (default: 8787).")
    parser.add_argument("--db", default=None,
                        help="SQLite file path, ':memory:', or a Postgres URL "
                             "(postgresql://...). Defaults to $MORPHDB_DATABASE_URL "
                             "or morphdb.sqlite3.")
    parser.add_argument("--version", action="version",
                        version=f"morphdb {__version__}")
    args = parser.parse_args(argv)

    try:
        serve(host=args.host, port=args.port, db_path=args.db)
    except OSError as e:
        sys.stderr.write(f"[morphdb] failed to start: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
