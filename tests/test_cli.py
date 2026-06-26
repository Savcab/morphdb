"""CLI subpackage: service state, dashboard snapshot, and skill installation.

These avoid spawning real server processes (kept fast + CI-stable); the
start/stop lifecycle is exercised by a manual smoke instead.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness                                          # noqa: E402
from morphdb import apps, db, objects, schema          # noqa: E402
from morphdb.cli import dashboard, service              # noqa: E402
from morphdb.cli import main as cli_main                # noqa: E402
from morphdb.cli import skill as skill_mod              # noqa: E402


class TestService(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MORPHDB_HOME")
        self.tmp = tempfile.mkdtemp()
        os.environ["MORPHDB_HOME"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MORPHDB_HOME", None)
        else:
            os.environ["MORPHDB_HOME"] = self._old

    def test_default_db_under_home(self):
        self.assertEqual(service.default_db(),
                         os.path.join(self.tmp, "data.sqlite3"))

    def test_status_not_running_when_no_meta(self):
        self.assertEqual(service.status(), {"running": False})

    def test_meta_roundtrip_and_clear(self):
        service.write_meta({"pid": 1, "host": "x", "port": 1})
        self.assertEqual(service.read_meta()["host"], "x")
        service.clear_meta()
        self.assertIsNone(service.read_meta())

    def test_alive(self):
        self.assertTrue(service._alive(os.getpid()))
        self.assertFalse(service._alive(999_999_999))
        self.assertFalse(service._alive(None))

    def test_status_reports_stale_pid(self):
        service.write_meta({"pid": 999_999_999, "host": "h", "port": 9, "db": "d"})
        st = service.status()
        self.assertFalse(st["running"])
        self.assertTrue(st["stale"])


class TestDashboardGather(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        db.init_db(self.path)

    def tearDown(self):
        db.init_db(":memory:")          # reset the global connection for other tests
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass

    def test_gather_reports_apps_types_counts(self):
        apps.register_app("a")
        apps.register_app("b")
        schema.upsert_type("a", "task", fields={"title": "string"})
        objects.create_object("a", "task", {"title": "x"})
        objects.create_object("a", "task", {"title": "y"})
        db.conn().execute("PRAGMA wal_checkpoint(FULL)")   # flush WAL for the ro reader

        data = dashboard.gather(self.path)
        by_app = {a["app"]: a for a in data["apps"]}
        self.assertEqual(set(by_app), {"a", "b"})
        task = by_app["a"]["types"][0]
        self.assertEqual(task["name"], "task")
        self.assertEqual(task["count"], 2)
        self.assertEqual([f["name"] for f in task["fields"]], ["title"])
        self.assertEqual(task["fields"][0]["type"], "string")
        self.assertEqual(by_app["b"]["types"], [])
        # render must not blow up and must mention the app
        self.assertIn("a", dashboard.render(data, self.path))

    def test_gather_on_empty_db_reports_error(self):
        empty = tempfile.mktemp(suffix=".sqlite3")
        import sqlite3
        sqlite3.connect(empty).close()         # a db with no MorphDB schema
        try:
            self.assertIn("error", dashboard.gather(empty))
        finally:
            os.remove(empty)


class TestInstallSkill(unittest.TestCase):
    def test_install_copies_skill_files(self):
        d = tempfile.mkdtemp()
        dest, existed = skill_mod.install_skill(claude_dir=d)
        self.assertFalse(existed)
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))
        self.assertEqual(dest, os.path.join(d, "skills", "morphdb"))

    def test_reinstall_is_idempotent(self):
        d = tempfile.mkdtemp()
        skill_mod.install_skill(claude_dir=d)
        dest, existed = skill_mod.install_skill(claude_dir=d)   # re-run overwrites
        self.assertTrue(existed)
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))


class TestLogs(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MORPHDB_HOME")
        self.tmp = tempfile.mkdtemp()
        os.environ["MORPHDB_HOME"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MORPHDB_HOME", None)
        else:
            os.environ["MORPHDB_HOME"] = self._old

    def _run(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main.main(argv)
        return rc, buf.getvalue()

    def test_missing_log(self):
        rc, out = self._run(["logs"])
        self.assertEqual(rc, 1)
        self.assertIn("No log yet", out)

    def test_shows_tail(self):
        with open(service.log_file(), "w") as f:
            f.write("line1\nline2\nline3\n")
        rc, out = self._run(["logs", "-n", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("line3", out)
        self.assertNotIn("line1", out)


class TestSchemaCli(unittest.TestCase):
    """The `morphdb app|schema|query` subcommands, driven against the in-process
    harness server (pointed at via $MORPHDB_HOST, so no real daemon is spawned)."""

    APP = "clitest"

    def setUp(self):
        harness.ensure_server()
        db.init_db(":memory:")
        self._env = {k: os.environ.get(k) for k in ("MORPHDB_HOST", "MORPHDB_APP")}
        os.environ["MORPHDB_HOST"] = harness.BASE
        os.environ.pop("MORPHDB_APP", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main.main(list(argv))
        return rc, out.getvalue()

    def _json(self, *argv):
        rc, out = self._run(*argv)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def test_register_add_field_list_and_query(self):
        self._json("app", "register", self.APP)
        self._json("schema", "add-field", "task", "title", "string", "--app", self.APP)
        self._json("schema", "add-field", "task", "done", "boolean",
                   "--default", "false", "--index", "--app", self.APP)
        self.assertIn("task", json.dumps(self._json("schema", "list", "--app", self.APP)))

        # the frontend writes object data over HTTP; query reads it back for debug
        harness.req("POST", "/objects/task",
                    {"title": "buy milk", "done": False}, app=self.APP)
        res = self._json("query", "task", "done=false", "--app", self.APP)
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["objects"][0]["title"], "buy milk")

    def test_morphdb_app_env_supplies_the_key(self):
        self._json("app", "register", self.APP)
        os.environ["MORPHDB_APP"] = self.APP            # no --app on the calls below
        self._json("schema", "add-field", "note", "body", "string")
        self.assertIn("note", json.dumps(self._json("schema", "list")))

    def test_add_and_drop_relation(self):
        self._json("app", "register", self.APP)
        self._json("schema", "add-field", "user", "name", "string", "--app", self.APP)
        self._json("schema", "add-relation", "task", "assignee", "--to", "user",
                   "--cardinality", "many_to_one", "--inverse", "tasks", "--app", self.APP)
        self.assertIn("assignee",
                      json.dumps(self._json("schema", "show", "task", "--app", self.APP)))
        self._json("schema", "drop-relation", "task", "assignee", "--app", self.APP)
        task = self._json("schema", "show", "task", "--app", self.APP)
        self.assertNotIn("assignee", json.dumps(task.get("relations", {})))

    def test_missing_app_key_exits(self):
        with self.assertRaises(SystemExit):
            self._run("schema", "list")                 # no --app, no $MORPHDB_APP

    def _build_sample_schema(self):
        self._json("app", "register", self.APP)
        self._json("schema", "add-field", "user", "name", "string", "--app", self.APP)
        self._json("schema", "add-field", "task", "title", "string", "--app", self.APP)
        self._json("schema", "add-field", "task", "done", "boolean",
                   "--default", "false", "--index", "--app", self.APP)
        self._json("schema", "add-relation", "task", "assignee", "--to", "user",
                   "--cardinality", "many_to_one", "--inverse", "tasks", "--app", self.APP)

    def _export_to_file(self):
        """Build the sample schema, export it, and write it to a temp file. Returns
        (path, exported_doc)."""
        self._build_sample_schema()
        exported = self._json("export-schema", self.APP)
        path = os.path.join(tempfile.mkdtemp(), "morphdb.schema.json")
        with open(path, "w") as f:
            json.dump(exported, f)
        return path, exported

    def test_init_creates_app_from_file(self):
        path, _ = self._export_to_file()
        self._json("app", "delete", self.APP)           # fresh backend

        res = self._json("init", path)
        self.assertEqual(res["app"], self.APP)
        self.assertEqual(res["status"], "created")

        task = self._json("schema", "show", "task", "--app", self.APP)
        self.assertIn("title", task["fields"])
        self.assertEqual(task["fields"]["done"]["default"], False)   # slim kept false
        self.assertTrue(task["fields"]["done"]["index"])
        self.assertEqual(task["relations"]["assignee"]["to"], "user")

    def test_init_clash_merges_and_keeps_data(self):
        """The core guarantee: re-init onto an existing app must NOT wipe it."""
        path, _ = self._export_to_file()
        harness.req("POST", "/objects/task", {"title": "keep me"}, app=self.APP)

        res = self._json("init", path)                  # app already exists
        self.assertEqual(res["status"], "merged")

        rows = self._json("query", "task", "--app", self.APP)
        self.assertEqual(rows["total"], 1)              # data survived
        self.assertEqual(rows["objects"][0]["title"], "keep me")

    def test_init_reset_rebuilds_clean(self):
        path, _ = self._export_to_file()
        harness.req("POST", "/objects/task", {"title": "wipe me"}, app=self.APP)

        res = self._json("init", path, "--reset")       # destructive path
        self.assertEqual(res["status"], "reset")

        rows = self._json("query", "task", "--app", self.APP)
        self.assertEqual(rows["total"], 0)              # objects gone, schema rebuilt

    def test_init_defaults_to_root_schema_file(self):
        path, _ = self._export_to_file()
        self._json("app", "delete", self.APP)
        cwd = os.getcwd()
        os.chdir(os.path.dirname(path))                 # holds morphdb.schema.json
        try:
            res = self._json("init")                    # no file arg
        finally:
            os.chdir(cwd)
        self.assertEqual(res["status"], "created")

    def test_init_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            self._run("init", os.path.join(tempfile.mkdtemp(), "nope.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
