"""Field index: the opt-in, index-backed accelerator for object field queries.

Engine-level tests (no HTTP) so we can inspect the ``field_index`` table directly
and EXPLAIN the query plan. Indexing is opt-in per field (``"index": true``):

  * correctness — index filtering/sorting agrees with read projection exactly,
    including defaults and post-retype staleness (lazy invalidation);
  * opt-in — only flagged fields are indexed; filtering/sorting an un-indexed (or
    json) field is a hard 400 telling the agent to mark it indexed;
  * lifecycle — writes keep rows in sync; toggling the flag backfills/drops one
    field; deletes cascade; backfill rebuilds.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morphdb import apps, db, fieldindex, objects, schema   # noqa: E402
from morphdb.errors import ApiError                          # noqa: E402

APP = "t"
TYPE = "thing"


def names(res):
    return sorted(o["name"] for o in res["objects"])


class Base(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")
        apps.register_app(APP)
        schema.upsert_type(APP, TYPE, fields={
            "name":   {"type": "string",   "index": True},
            "age":    {"type": "number",   "index": True},
            "active": {"type": "boolean",  "index": True},
            "joined": {"type": "datetime", "index": True},
            "status": {"type": "string", "default": "active", "index": True},
            "note":   "string",     # un-indexed (shorthand) -> not filterable
            "meta":   "json",       # json -> never indexable
        })

    def rows_for(self, guid):
        cur = db.conn().execute(
            "SELECT field_name, str_val, num_val, bool_val FROM field_index "
            "WHERE object_id = ?", (guid,))
        return {r["field_name"]: (r["str_val"], r["num_val"], r["bool_val"])
                for r in cur.fetchall()}

    def mk(self, **data):
        return objects.create_object(APP, TYPE, data)

    def ls(self, filters=None, **kw):
        return objects.list_objects(APP, TYPE, filters or {}, **kw)


class TestRowMaintenance(Base):
    def test_only_indexed_scalars_get_rows(self):
        a = self.mk(name="ann", age=30, active=True,
                    joined="2024-01-01T00:00:00Z", note="hi", meta={"x": 1})
        fi = self.rows_for(a["_guid"])
        self.assertEqual(sorted(fi), ["active", "age", "joined", "name", "status"])
        self.assertNotIn("note", fi)    # un-indexed scalar -> no row
        self.assertNotIn("meta", fi)    # json -> no row
        self.assertEqual(fi["age"], (None, 30, None))
        self.assertEqual(fi["name"], ("ann", None, None))
        self.assertEqual(fi["active"], (None, None, 1))
        self.assertEqual(fi["joined"][0], "2024-01-01T00:00:00.000000Z")

    def test_default_materialized_on_create(self):
        a = self.mk(name="ann")
        self.assertEqual(self.rows_for(a["_guid"])["status"], ("active", None, None))

    def test_update_rewrites_rows(self):
        a = self.mk(name="ann", age=30)
        objects.upsert_object(APP, TYPE, a["_guid"], {"age": 31, "name": "annie"})
        fi = self.rows_for(a["_guid"])
        self.assertEqual(fi["age"], (None, 31, None))
        self.assertEqual(fi["name"], ("annie", None, None))

    def test_null_indexed_value_not_rowed(self):
        schema.upsert_type(APP, TYPE, fields={
            "nick": {"type": "string", "index": True}}, merge=True)
        a = self.mk(name="ann", nick=None)
        self.assertNotIn("nick", self.rows_for(a["_guid"]))


class TestFilters(Base):
    def setUp(self):
        super().setUp()
        self.a = self.mk(name="ann", age=30, active=True, joined="2024-01-01T00:00:00Z")
        self.b = self.mk(name="bob", age=17, active=False, joined="2020-06-01T00:00:00Z")
        self.c = self.mk(name="cat", age=30, active=True, joined="2022-03-01T00:00:00Z")

    def test_eq(self):
        self.assertEqual(names(self.ls({"age": "30"})), ["ann", "cat"])

    def test_ne(self):
        self.assertEqual(names(self.ls({"age__ne": "30"})), ["bob"])

    def test_gt_gte_lt_lte(self):
        self.assertEqual(names(self.ls({"age__gt": "17"})), ["ann", "cat"])
        self.assertEqual(names(self.ls({"age__gte": "17"})), ["ann", "bob", "cat"])
        self.assertEqual(names(self.ls({"age__lt": "30"})), ["bob"])
        self.assertEqual(names(self.ls({"age__lte": "17"})), ["bob"])

    def test_boolean(self):
        self.assertEqual(names(self.ls({"active": "true"})), ["ann", "cat"])
        self.assertEqual(names(self.ls({"active": "false"})), ["bob"])

    def test_contains_case_insensitive(self):
        self.assertEqual(names(self.ls({"name__contains": "A"})), ["ann", "cat"])

    def test_in(self):
        self.assertEqual(names(self.ls({"name__in": "ann,bob"})), ["ann", "bob"])

    def test_in_empty_matches_nothing(self):
        self.assertEqual(names(self.ls({"name__in": ""})), [])

    def test_exists(self):
        self.assertEqual(names(self.ls({"status__exists": "true"})), ["ann", "bob", "cat"])
        self.assertEqual(names(self.ls({"status__exists": "false"})), [])

    def test_datetime_range(self):
        self.assertEqual(names(self.ls({"joined__gt": "2021-01-01T00:00:00Z"})),
                         ["ann", "cat"])

    def test_compose_field_and_field(self):
        self.assertEqual(names(self.ls({"age": "30", "active": "true"})), ["ann", "cat"])

    def test_big_int_exact(self):
        big = 2 ** 60
        d = self.mk(name="big", age=big)
        self.assertEqual(names(self.ls({"age": str(big)})), ["big"])
        self.assertEqual(self.rows_for(d["_guid"])["age"], (None, big, None))


class TestOptIn(Base):
    """The (b) policy: filter/sort requires an explicit index."""

    def setUp(self):
        super().setUp()
        self.mk(name="ann", age=30, note="alpha")
        self.mk(name="bob", age=17, note="beta")

    def _err(self, fn):
        with self.assertRaises(ApiError) as cm:
            fn()
        self.assertEqual(cm.exception.status, 400)
        return str(cm.exception)

    def test_filter_unindexed_field_errors(self):
        msg = self._err(lambda: self.ls({"note": "alpha"}))
        self.assertIn("not indexed", msg)
        self.assertIn("index", msg)

    def test_sort_unindexed_field_errors(self):
        self.assertIn("not indexed", self._err(lambda: self.ls({}, sort="note")))

    def test_filter_json_field_errors(self):
        self.assertIn("json", self._err(lambda: self.ls({"meta": "x"})))

    def test_json_field_cannot_be_indexed(self):
        with self.assertRaises(ApiError):
            schema.upsert_type(APP, TYPE, fields={
                "blob": {"type": "json", "index": True}}, merge=True)

    def test_enable_index_backfills_existing(self):
        # note is un-indexed and has live data; turn the flag on -> backfill
        self._err(lambda: self.ls({"note": "alpha"}))     # errors first
        schema.upsert_type(APP, TYPE, fields={
            "note": {"type": "string", "index": True}}, merge=True)
        # existing objects are now findable without being rewritten
        self.assertEqual(names(self.ls({"note": "alpha"})), ["ann"])
        self.assertEqual(names(self.ls({"note__contains": "et"})), ["bob"])

    def test_disable_index_drops_rows_and_errors(self):
        self.assertEqual(names(self.ls({"age": "30"})), ["ann"])    # works while indexed
        schema.upsert_type(APP, TYPE, fields={
            "age": {"type": "number", "index": False}}, merge=True)
        rows = db.conn().execute(
            "SELECT COUNT(*) n FROM field_index WHERE field_name='age'").fetchone()["n"]
        self.assertEqual(rows, 0)                                   # rows dropped
        self._err(lambda: self.ls({"age": "30"}))                   # now rejected


class TestIndexBacked(Base):
    def test_numeric_filter_uses_index(self):
        plan = db.conn().execute(
            "EXPLAIN QUERY PLAN SELECT object_id FROM field_index "
            "WHERE app=? AND object_type=? AND field_name=? AND num_val>?",
            (APP, TYPE, "age", 5)).fetchall()
        text = " ".join(r["detail"] for r in plan)
        self.assertIn("field_index", text)
        self.assertIn("INDEX", text)
        self.assertNotIn("SCAN field_index", text)

    def test_string_filter_uses_index(self):
        plan = db.conn().execute(
            "EXPLAIN QUERY PLAN SELECT object_id FROM field_index "
            "WHERE app=? AND object_type=? AND field_name=? AND str_val=?",
            (APP, TYPE, "name", "ann")).fetchall()
        text = " ".join(r["detail"] for r in plan)
        self.assertIn("field_index", text)
        self.assertIn("INDEX", text)
        self.assertNotIn("SCAN field_index", text)


class TestLazySchemaEdits(Base):
    def test_retype_clears_wrong_type_rows(self):
        a = self.mk(name="ann", age=30)
        # number -> string, still indexed: reconcile re-scans; 30 is not a string,
        # so its num_val row is cleared and ann reads age as default (None)
        schema.upsert_type(APP, TYPE, fields={
            "name": {"type": "string", "index": True},
            "age": {"type": "string", "index": True}}, merge=False)
        self.assertNotIn("age", self.rows_for(a["_guid"]))
        self.assertEqual(names(self.ls({"age": "30"})), [])
        self.assertIsNone(objects.get_object(APP, a["_guid"])["age"])
        # rewriting with a real string re-indexes into str_val
        objects.upsert_object(APP, TYPE, a["_guid"], {"age": "thirty"})
        self.assertEqual(names(self.ls({"age": "thirty"})), ["ann"])

    def test_added_field_default_is_query_visible(self):
        a = self.mk(name="ann")
        b = self.mk(name="bob")
        schema.upsert_type(APP, TYPE, fields={
            "tier": {"type": "string", "default": "free", "index": True}}, merge=True)
        self.assertNotIn("tier", self.rows_for(a["_guid"]))   # not materialized
        self.assertEqual(names(self.ls({"tier": "free"})), ["ann", "bob"])
        self.assertEqual(names(self.ls({"tier__ne": "free"})), [])
        objects.upsert_object(APP, TYPE, b["_guid"], {"tier": "pro"})
        self.assertEqual(names(self.ls({"tier": "free"})), ["ann"])
        self.assertEqual(names(self.ls({"tier": "pro"})), ["bob"])

    def test_changing_default_is_instant(self):
        self.mk(name="ann")
        self.mk(name="bob")
        schema.upsert_type(APP, TYPE, fields={
            "tier": {"type": "string", "default": "free", "index": True}}, merge=True)
        self.assertEqual(names(self.ls({"tier": "free"})), ["ann", "bob"])
        schema.upsert_type(APP, TYPE, fields={
            "tier": {"type": "string", "default": "gold", "index": True}}, merge=True)
        self.assertEqual(names(self.ls({"tier": "free"})), [])
        self.assertEqual(names(self.ls({"tier": "gold"})), ["ann", "bob"])


class TestSort(Base):
    def test_sort_by_indexed_field(self):
        self.mk(name="ann", age=30)
        self.mk(name="bob", age=17)
        self.mk(name="cat", age=99)
        asc = [o["name"] for o in self.ls({}, sort="age", order="asc")["objects"]]
        self.assertEqual(asc, ["bob", "ann", "cat"])
        desc = [o["name"] for o in self.ls({}, sort="age", order="desc")["objects"]]
        self.assertEqual(desc, ["cat", "ann", "bob"])

    def test_sort_falls_back_to_default(self):
        schema.upsert_type(APP, TYPE, fields={
            "rank": {"type": "number", "default": 50, "index": True}}, merge=True)
        a = self.mk(name="ann")
        objects.upsert_object(APP, TYPE, a["_guid"], {})       # rank stays absent -> 50
        b = self.mk(name="bob")
        objects.upsert_object(APP, TYPE, b["_guid"], {"rank": 10})
        c = self.mk(name="cat")
        objects.upsert_object(APP, TYPE, c["_guid"], {"rank": 90})
        order = [o["name"] for o in self.ls({}, sort="rank", order="asc")["objects"]]
        self.assertEqual(order, ["bob", "ann", "cat"])         # 10, 50(default), 90


class TestDeleteCascades(Base):
    def _count(self, **scope):
        sql = "SELECT COUNT(*) n FROM field_index"
        params = []
        if scope:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in scope)
            params = list(scope.values())
        return db.conn().execute(sql, params).fetchone()["n"]

    def test_delete_object_cascades(self):
        a = self.mk(name="ann", age=30)
        self.assertGreater(self._count(object_id=a["_guid"]), 0)
        objects.delete_object(APP, a["_guid"])
        self.assertEqual(self._count(object_id=a["_guid"]), 0)

    def test_delete_type_cascades(self):
        self.mk(name="ann", age=30)
        self.mk(name="bob", age=17)
        self.assertGreater(self._count(), 0)
        schema.delete_type(APP, TYPE)
        self.assertEqual(self._count(), 0)

    def test_delete_app_cascades(self):
        self.mk(name="ann", age=30)
        self.assertGreater(self._count(), 0)
        apps.delete_app(APP)
        self.assertEqual(self._count(), 0)


class TestBackfill(Base):
    def test_backfill_rebuilds_from_blobs(self):
        self.mk(name="ann", age=30)
        self.mk(name="bob", age=17)
        with db.transaction() as c:
            c.execute("DELETE FROM field_index")
        self.assertEqual(names(self.ls({"age": "30"})), [])    # index emptied
        with db.transaction() as c:
            scanned = fieldindex.backfill(c)
        self.assertEqual(scanned, 2)
        self.assertEqual(names(self.ls({"age": "30"})), ["ann"])

    def test_backfill_scoped_to_app(self):
        apps.register_app("u")
        schema.upsert_type("u", TYPE, fields={
            "name": "string", "age": {"type": "number", "index": True}})
        objects.create_object("u", TYPE, {"name": "zed", "age": 5})
        self.mk(name="ann", age=30)
        with db.transaction() as c:
            c.execute("DELETE FROM field_index")
            fieldindex.backfill(c, app=APP)
        self.assertEqual(names(self.ls({"age": "30"})), ["ann"])
        u_rows = db.conn().execute(
            "SELECT COUNT(*) n FROM field_index WHERE app='u'").fetchone()["n"]
        self.assertEqual(u_rows, 0)


class TestRelationsOrthogonal(Base):
    def test_relations_are_not_field_indexed(self):
        schema.upsert_type(APP, "owner", fields={"name": "string"}, relations={
            "things": {"to": TYPE, "cardinality": "one_to_many", "inverse": "owner"}})
        o = objects.create_object(APP, "owner", {"name": "o"})
        t = objects.create_object(APP, TYPE, {"name": "ann", "age": 30, "owner": o["_guid"]})
        fi = self.rows_for(t["_guid"])
        self.assertNotIn("owner", fi)      # relation key is not a field row
        self.assertIn("age", fi)           # ordinary indexed field still present


if __name__ == "__main__":
    unittest.main()
