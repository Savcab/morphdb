"""MorphDB command-line interface — process management + admin dashboard.

Intentionally separate from the core engine (db / schema / objects / server):
this package only *orchestrates* a server process and offers a read-only admin
view. It never changes how the core stores or serves data.

Commands (see :mod:`morphdb.cli.main`):

    morphdb               start the server in the background (alias of `start`)
    morphdb start         same, explicit
    morphdb status        is it running? where? how many apps?
    morphdb stop          stop the background server
    morphdb logs          show the server log (-f to follow)
    morphdb run           run in the foreground (blocking; for dev)
    morphdb dashboard     run a read-only web view of every app in the background
    morphdb dashboard stop   stop the background dashboard (also: `dashboard status`)
    morphdb mcp           run the MCP server (stdio; spawned by Claude Code, not you)
    morphdb install-skill install/update the bundled Claude Code skill

The ``mcp`` command is a thin HTTP *client* of the running backend daemon (it
auto-starts the daemon if needed). It exposes schema + app operations to a coding
agent as MCP tools, so the agent calls real tools instead of shelling out to the
bundled schema script. It is pure stdlib — no MCP SDK — so the package stays
dependency-free.

Storage: the local server keeps data in a per-user SQLite file at
``~/.morphdb/data.sqlite3`` (override the file with ``--db``, or move the state
dir with ``$MORPHDB_HOME``). To talk to a MorphDB hosted somewhere else instead
of a local one, point *clients* at it with ``$MORPHDB_HOST`` (a full URL) — that
is a client-side setting, not a database connection string; the engine is always
SQLite.
"""
