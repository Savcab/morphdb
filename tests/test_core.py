"""Core: type schema CRUD, object CRUD, the query layer, and lazy morphing."""

import unittest

from harness import Base, req


class TestTypeSchema(Base):
    def test_create_with_shorthand_and_rich_fields(self):
        doc = self.put_type("task", fields={
            "title": "string",
            "done": {"type": "boolean", "default": False},
            "priority": "number",
        })
        self.assertEqual(doc["fields"]["done"]["default"], False)
        self.assertEqual(doc["relations"], {})
        self.assertEqual(doc["inverse_relations"], {})

    def test_bare_field_map_body(self):
        st, b, _ = self.put("/schema/note", {"text": "string"})
        self.assertEqual(st, 200, b)
        self.assertIn("text", b["fields"])

    def test_get_missing_type_404(self):
        st, _, _ = self.get("/schema/nope")
        self.assertEqual(st, 404)

    def test_list_schema(self):
        self.put_type("a", fields={"x": "string"})
        self.put_type("b", fields={"y": "number"})
        st, b, _ = self.get("/schema")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(t["name"] for t in b["types"]), ["a", "b"])

    def test_merge_adds_without_dropping(self):
        self.put_type("task", fields={"title": "string"})
        self.put_type("task", merge=True, fields={"done": "boolean"})
        doc = self.put_type("task", merge=True, fields={"priority": "number"})
        self.assertEqual(set(doc["fields"]), {"title", "done", "priority"})

    def test_replace_drops_omitted_fields(self):
        self.put_type("task", fields={"title": "string", "done": "boolean"})
        doc = self.put_type("task", fields={"title": "string"})   # merge=false
        self.assertEqual(set(doc["fields"]), {"title"})

    def test_absent_fields_left_untouched(self):
        # A PUT that only sets relations must not wipe fields.
        self.put_type("user", fields={"name": "string"})
        self.put_type("task", fields={"title": "string"})
        self.put_type("task", merge=True, relations={
            "owner": {"to": "user", "cardinality": "many_to_one", "inverse": "tasks"}})
        st, b, _ = self.get("/schema/task")
        self.assertIn("title", b["fields"])


class TestObjectCrud(Base):
    def setUp(self):
        super().setUp()
        self.put_type("task", fields={
            "title": "string", "done": {"type": "boolean", "default": False},
            "priority": "number"})

    def test_create_read_has_system_fields(self):
        o = self.create("task", {"title": "x", "priority": 1})
        for k in ("_guid", "_type", "_created_at", "_updated_at"):
            self.assertIn(k, o)
        self.assertEqual(o["_type"], "task")
        self.assertEqual(o["done"], False)            # default materialized
        got = self.read("task", o["_guid"])
        self.assertEqual(got["title"], "x")

    def test_unknown_field_rejected(self):
        st, b, _ = self.post("/objects/task", {"title": "x", "nope": 1})
        self.assertEqual(st, 400)
        self.assertIn("nope", b["error"]["message"])

    def test_patch_merges_put_replaces(self):
        o = self.create("task", {"title": "x", "priority": 5})
        self.patch(f"/objects/task/{o['_guid']}", {"done": True})
        got = self.read("task", o["_guid"])
        self.assertEqual(got["done"], True)
        self.assertEqual(got["priority"], 5)          # untouched by patch
        # PUT replaces: omitted priority falls back to default (none -> null)
        self.put(f"/objects/task/{o['_guid']}", {"title": "y"})
        got = self.read("task", o["_guid"])
        self.assertEqual(got["title"], "y")
        self.assertIsNone(got["priority"])

    def test_put_and_patch_create_if_absent(self):
        st, b, _ = self.put("/objects/task/task_made_up", {"title": "z"})
        self.assertEqual(st, 200, b)
        self.assertEqual(self.read("task", "task_made_up")["title"], "z")

    def test_delete(self):
        o = self.create("task", {"title": "x"})
        st, _, _ = self.delete(f"/objects/task/{o['_guid']}")
        self.assertEqual(st, 200)
        st, _, _ = self.get(f"/objects/task/{o['_guid']}")
        self.assertEqual(st, 404)

    def test_read_by_guid_alone(self):
        o = self.create("task", {"title": "x"})
        st, b, _ = self.get(f"/object/{o['_guid']}")
        self.assertEqual(st, 200)
        self.assertEqual(b["title"], "x")

    def test_type_mismatch_on_typed_read_404(self):
        self.put_type("note", fields={"text": "string"})
        o = self.create("task", {"title": "x"})
        st, _, _ = self.get(f"/objects/note/{o['_guid']}")
        self.assertEqual(st, 404)

    def test_non_object_body_rejected(self):
        st, _, _ = req("POST", "/objects/task", raw_body=b'["not","an","object"]')
        self.assertEqual(st, 400)


class TestQuery(Base):
    def setUp(self):
        super().setUp()
        self.put_type("task", fields={
            "title": "string", "done": "boolean", "priority": "number"})
        for t, d, p in [("a", False, 1), ("b", True, 3), ("c", False, 2)]:
            self.create("task", {"title": t, "done": d, "priority": p})

    def test_eq_and_total(self):
        st, b, _ = self.get("/objects/task?done=false")
        self.assertEqual(st, 200)
        self.assertEqual(b["total"], 2)
        self.assertTrue(all(o["done"] is False for o in b["objects"]))

    def test_operators(self):
        self.assertEqual(self.total("/objects/task?priority__gte=2"), 2)
        self.assertEqual(self.total("/objects/task?priority__lt=2"), 1)
        self.assertEqual(self.total("/objects/task?priority__ne=1"), 2)
        self.assertEqual(self.total("/objects/task?title__contains=b"), 1)
        self.assertEqual(self.total("/objects/task?priority__in=1,3"), 2)
        self.assertEqual(self.total("/objects/task?title__exists=true"), 3)

    def test_sort_and_pagination(self):
        st, b, _ = self.get("/objects/task?sort=priority&order=asc&limit=2")
        self.assertEqual([o["priority"] for o in b["objects"]], [1, 2])
        self.assertEqual(b["total"], 3)
        st, b2, _ = self.get("/objects/task?sort=priority&order=asc&limit=2&offset=2")
        self.assertEqual([o["priority"] for o in b2["objects"]], [3])

    def test_unknown_filter_field_rejected(self):
        st, _, _ = self.get("/objects/task?bogus=1")
        self.assertEqual(st, 400)

    def test_bad_order_and_negative_limit(self):
        self.assertEqual(self.get("/objects/task?order=sideways")[0], 400)
        self.assertEqual(self.get("/objects/task?limit=-1")[0], 400)


class TestLazyMorph(Base):
    def test_add_field_to_existing_rows_reads_null(self):
        self.put_type("t", fields={"a": "string"})
        o = self.create("t", {"a": "x"})
        self.put_type("t", merge=True, fields={"b": "number"})
        self.assertIsNone(self.read("t", o["_guid"])["b"])

    def test_retype_reads_as_unset_and_query_agrees(self):
        # purely lazy: a value left at the old type reads as unset after retype
        self.put_type("t", fields={"v": "string"})
        o = self.create("t", {"v": "42"})
        self.put_type("t", merge=True, fields={"v": "number"})
        self.assertIsNone(self.read("t", o["_guid"])["v"])
        self.assertEqual(self.total("/objects/t?v=42"), 0)
        self.assertEqual(self.total("/objects/t?v__gt=0"), 0)
        # rewriting sets the value for the new type
        self.patch(f"/objects/t/{o['_guid']}", {"v": 7})
        self.assertEqual(self.read("t", o["_guid"])["v"], 7)
        self.assertEqual(self.total("/objects/t?v=7"), 1)

    def test_drop_field_hides_then_readd_recovers(self):
        self.put_type("t", fields={"a": "string", "b": "string"})
        o = self.create("t", {"a": "x", "b": "keep"})
        self.put_type("t", fields={"a": "string"})        # replace drops b
        self.assertNotIn("b", self.read("t", o["_guid"]))
        self.put_type("t", merge=True, fields={"b": "string"})   # re-add same type
        self.assertEqual(self.read("t", o["_guid"])["b"], "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
