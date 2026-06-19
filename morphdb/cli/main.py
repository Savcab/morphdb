"""``morphdb`` console-script entry point: a small process/admin CLI.

    morphdb            start the server in the background (alias of `start`)
    morphdb start      start the server in the background
    morphdb status     show whether it is running, where, and how many apps
    morphdb stop       stop the background server
    morphdb run        run the server in the foreground (blocking)
    morphdb dashboard  open the read-only admin dashboard

``python -m morphdb`` remains the plain foreground server (what `start` and the
skill spawn under the hood); this CLI only wraps it.
"""

import argparse
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
    serve_fg(host=args.host, port=args.port, db_path=args.db or service.default_db())
    return 0


def cmd_status(args):
    print(_fmt_status(service.status()))
    return 0


def cmd_stop(args):
    print("MorphDB stopped." if service.stop() else "MorphDB was not running.")
    return 0


def cmd_dashboard(args):
    dashboard.serve(args.db or service.default_db(), port=args.port,
                    open_browser=not args.no_open)
    return 0


def cmd_install_skill(args):
    try:
        dest = skill_mod.install_skill(project=args.project, force=args.force)
    except FileExistsError as e:
        print(f"Skill already installed at {e}. Re-run with --force to overwrite.")
        return 1
    except (FileNotFoundError, OSError) as e:
        print(f"Could not install skill: {e}")
        return 1
    where = "this project" if args.project else "your home (~/.claude)"
    print(f"Installed the 'morphdb' Claude skill to {dest}\n"
          f"  ({where}). Restart Claude Code (or reload skills) to pick it up.")
    return 0


def _add_server_opts(sp):
    sp.add_argument("--host", default=service.DEFAULT_HOST,
                    help=f"bind host (default {service.DEFAULT_HOST})")
    sp.add_argument("--port", type=int, default=service.DEFAULT_PORT,
                    help=f"bind port (default {service.DEFAULT_PORT})")
    sp.add_argument("--db", default=None,
                    help="SQLite path or :memory: (default ~/.morphdb/data.sqlite3)")


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

    sp = sub.add_parser("dashboard", help="open the read-only admin dashboard")
    sp.add_argument("--port", type=int, default=8788, help="dashboard port (default 8788)")
    sp.add_argument("--db", default=None, help="database to inspect (default the server's)")
    sp.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("install-skill",
                        help="install the MorphDB skill into Claude Code")
    sp.add_argument("--project", nargs="?", const=".", default=None,
                    metavar="DIR",
                    help="install into a project's .claude (DIR, default cwd) "
                         "instead of ~/.claude")
    sp.add_argument("--force", action="store_true", help="overwrite if it exists")
    sp.set_defaults(func=cmd_install_skill)

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
