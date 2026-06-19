"""CLI subpackage: service state, dashboard snapshot, and skill installation.

These avoid spawning real server processes (kept fast + CI-stable); the
start/stop lifecycle is exercised by a manual smoke instead.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morphdb import apps, db, objects, schema          # noqa: E402
from morphdb.cli import dashboard, service              # noqa: E402
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
        self.assertIn("title", task["fields"])
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
        dest = skill_mod.install_skill(claude_dir=d)
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))
        self.assertTrue(os.path.isfile(
            os.path.join(dest, "scripts", "morphdb_schema.py")))
        # name + location
        self.assertEqual(os.path.basename(dest), "morphdb")
        self.assertEqual(dest, os.path.join(d, "skills", "morphdb"))

    def test_refuses_without_force_then_overwrites(self):
        d = tempfile.mkdtemp()
        skill_mod.install_skill(claude_dir=d)
        with self.assertRaises(FileExistsError):
            skill_mod.install_skill(claude_dir=d)
        # force overwrites cleanly
        dest = skill_mod.install_skill(claude_dir=d, force=True)
        self.assertTrue(os.path.isfile(os.path.join(dest, "SKILL.md")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
