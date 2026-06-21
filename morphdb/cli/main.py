"""``morphdb`` console-script entry point: a small process/admin CLI.

    morphdb               start the server in the background (alias of `start`)
    morphdb start         start the server in the background
    morphdb status        show whether it is running, where, and how many apps
    morphdb stop          stop the background server
    morphdb logs          show the background server's log (-f to follow)
    morphdb run           run the server in the foreground (blocking)
    morphdb dashboard     open the read-only admin dashboard
    morphdb mcp           run the MCP server (stdio; spawned by Claude Code)
    morphdb install-skill install the bundled Claude Code skill
    morphdb reindex       rebuild the field-value index from object data

``python -m morphdb`` remains the plain foreground server (what `start` and the
skill spawn under the hood); this CLI only wraps it.
"""

import argparse
import os
import sys

from . import dashboard, service
from . import skill as skill_mod


def _fmt_status(st):
    if not st.get("running"):
        msg = "MorphDB is not running."
        if st.get("stale"):
            msg += "  (cleared a stale pid file)"
        return msg
    health = "healthy" if st.get("healthy") else "starting / not responding yet"
    lines = [
        f"MorphDB is running (pid {st['pid']}) at http://{st['host']}:{st['port']}  [{health}]",
        f"  db:   {st['db']}",
    ]
    n = service.app_count(st.get("db"))
    if n is not None:
        lines.append(f"  apps: {n}")
    return "\n".join(lines)


def cmd_start(args):
    st, _ = service.start(args.host, args.port, args.db)
    print(_fmt_status(st))
    if not st.get("running"):
        print(f"  (server exited on startup — check the log: {service.log_file()})")
        return 1
    return 0


def cmd_run(args):
    from ..server import serve as serve_fg
    serve_fg(host=args.host, port=args.port, db_path=service.resolve_target(args.db))
    return 0


def cmd_status(args):
    print(_fmt_status(service.status()))
    from . import mcp
    print(f"  mcp:  {mcp.registration_summary()}")
    return 0


def cmd_mcp(args):
    from . import mcp
    return mcp.serve()


def cmd_stop(args):
    print("MorphDB stopped." if service.stop() else "MorphDB was not running.")
    return 0


def cmd_logs(args):
    path = service.log_file()
    if not os.path.exists(path):
        print(f"No log yet at {path}. Has the server run? Try `morphdb start`.")
        return 1
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
    tail = lines[-args.lines:] if args.lines and args.lines > 0 else lines
    sys.stdout.write("".join(tail))
    if tail and not tail[-1].endswith("\n"):
        sys.stdout.write("\n")
    if args.follow:
        _follow(path)
    return 0


def _follow(path):
    """Stream new lines appended to the log, like `tail -f`, until Ctrl-C."""
    import time
    with open(path, "r", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            pass


def cmd_dashboard(args):
    dashboard.serve(service.resolve_target(args.db), port=args.port,
                    open_browser=not args.no_open)
    return 0


def cmd_install_skill(args):
    try:
        dest, existed = skill_mod.install_skill(project=args.project)
    except (FileNotFoundError, OSError) as e:
        print(f"Could not install skill: {e}")
        return 1
    verb = "Updated" if existed else "Installed"
    where = "this project" if args.project else "~/.claude"
    print(f"{verb} the 'morphdb' Claude skill at {dest} ({where}).\n"
          f"  Restart Claude Code (or reload skills) to pick it up.")
    return 0


def cmd_reindex(args):
    from .. import fieldindex
    from ..db import init_db, transaction
    path = service.resolve_target(args.db)
    init_db(path)
    with transaction() as c:
        n = fieldindex.backfill(c, app=args.app)
    scope = f" in app '{args.app}'" if args.app else ""
    print(f"Reindexed field_index for {n} object(s){scope}.")
    return 0


def _add_server_opts(sp):
    sp.add_argument("--host", default=service.DEFAULT_HOST,
                    help=f"bind host (default {service.DEFAULT_HOST})")
    sp.add_argument("--port", type=int, default=service.DEFAULT_PORT,
                    help=f"bind port (default {service.DEFAULT_PORT})")
    sp.add_argument("--db", default=None,
                    help="SQLite path, :memory:, or a Postgres URL "
                         "(postgresql://...). Default $MORPHDB_DATABASE_URL or "
                         "~/.morphdb/data.sqlite3")


def build_parser():
    from .. import __version__
    p = argparse.ArgumentParser(
        prog="morphdb",
        description="MorphDB — a multi-tenant backend for vibe-coded sites. "
                    "Manage the local server and view your apps.")
    p.add_argument("--version", action="version", version=f"morphdb {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("start", help="start the server in the background")
    _add_server_opts(sp)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("run", help="run the server in the foreground (blocking)")
    _add_server_opts(sp)
    sp.set_defaults(func=cmd_run)

    sub.add_parser("status", help="show whether the server is running"
                   ).set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop the background server"
                   ).set_defaults(func=cmd_stop)

    sp = sub.add_parser("logs", help="show the background server's log")
    sp.add_argument("-n", "--lines", type=int, default=200,
                    help="number of trailing lines to show (default 200)")
    sp.add_argument("-f", "--follow", action="store_true",
                    help="stream new log lines until Ctrl-C")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("dashboard", help="open the read-only admin dashboard")
    sp.add_argument("--port", type=int, default=8788, help="dashboard port (default 8788)")
    sp.add_argument("--db", default=None, help="database to inspect (default the server's)")
    sp.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    sp.set_defaults(func=cmd_dashboard)

    sub.add_parser("mcp", help="run the MCP server over stdio (Claude Code spawns "
                   "this; you don't run it directly)").set_defaults(func=cmd_mcp)

    sp = sub.add_parser("install-skill",
                        help="install/update the bundled Claude Code skill")
    sp.add_argument("--project", nargs="?", const=".", default=None,
                    metavar="DIR",
                    help="install into a project's .claude (DIR, default cwd) "
                         "instead of ~/.claude")
    sp.set_defaults(func=cmd_install_skill)

    sp = sub.add_parser("reindex",
                        help="rebuild the field-value index from object data "
                             "(maintenance/repair; normally automatic on upgrade)")
    sp.add_argument("--db", default=None,
                    help="database to reindex (default the server's)")
    sp.add_argument("--app", default=None,
                    help="limit to one app key (default: all apps)")
    sp.set_defaults(func=cmd_reindex)

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:                      # bare `morphdb` => start in the background
        argv = ["start"]
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
