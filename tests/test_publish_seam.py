"""The db-level change-publish seam (PR1): records reach the hook post-commit,
in commit order, only for interested types, and never on rollback.

Exercises db.set_publish_hook / stage_change / interested directly against the
write handlers — no streaming module yet.
"""

import unittest

from morphdb import apps, db, objects, schema


class SeamBase(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")
        self.records = []
        apps.register_app("a")
        schema.upsert_type("a", "task", fields={"title": {"type": "string"},
                                                "done": {"type": "boolean"}})
        # Install the hook after setup writes so the change seq starts clean.
        db.set_publish_hook(self.records.extend, interested=lambda app, types: True)

    def tearDown(self):
        db.set_publish_hook(None)


class TestSeam(SeamBase):
    def test_create_stages_one_record_post_commit(self):
        obj = objects.create_object("a", "task", {"title": "ship", "done": False})
        self.assertEqual(len(self.records), 1)
        r = self.records[0]
        self.assertEqual((r["app"], r["type"], r["verb"]), ("a", "task", "create"))
        self.assertEqual(r["guid"], obj["_guid"])
        self.assertEqual(r["new_body"]["title"], "ship")
        self.assertEqual(r["touched"], [])
        self.assertEqual(r["seq"], 1)  # first published change

    def test_seq_is_monotonic_commit_order(self):
        objects.create_object("a", "task", {"title": "one"})
        objects.create_object("a", "task", {"title": "two"})
        self.assertEqual([r["seq"] for r in self.records], [1, 2])

    def test_update_and_delete_verbs(self):
        obj = objects.create_object("a", "task", {"title": "x"})
        g = obj["_guid"]
        objects.upsert_object("a", "task", g, {"title": "y"}, partial=True)
        objects.delete_object("a", g)
        verbs = [r["verb"] for r in self.records]
        self.assertEqual(verbs, ["create", "update", "delete"])
        self.assertIsNone(self.records[-1]["new_body"])

    def test_rollback_publishes_only_synthetic_dirty_record(self):
        # A write that raises after staging must publish no object record; a
        # single dirty-only record replaces the batch.
        try:
            with db.store_transaction() as s:
                s.insert_object("g1", "a", "task", '{"title":"x"}', "t", "t")
                db.stage_change({"app": "a", "type": "task", "guid": "g1",
                                 "verb": "create", "new_body": {}, "touched": []})
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(len(self.records), 1)
        self.assertIn("dirty", self.records[0])
        self.assertIn(["a", "task"], self.records[0]["dirty"])

    def test_uninterested_write_stages_nothing(self):
        db.set_publish_hook(self.records.extend,
                            interested=lambda app, types: False)
        objects.create_object("a", "task", {"title": "quiet"})
        self.assertEqual(self.records, [])

    def test_no_hook_means_no_staging(self):
        db.set_publish_hook(None)
        objects.create_object("a", "task", {"title": "z"})  # must not raise
        self.assertEqual(self.records, [])


class TestRelationTouched(SeamBase):
    def setUp(self):
        super().setUp()
        schema.upsert_type("a", "user", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "task", merge=True, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one",
                         "inverse": "tasks"}})
        self.records.clear()

    def test_relation_write_touches_neighbor(self):
        u = objects.create_object("a", "user", {"name": "ann"})
        self.records.clear()
        objects.create_object("a", "task", {"title": "t", "assignee": u["_guid"]})
        r = self.records[0]
        self.assertIn(["user", u["_guid"]], r["touched"])

    def test_relist_same_neighbor_is_not_touched(self):
        u = objects.create_object("a", "user", {"name": "ann"})
        t = objects.create_object("a", "task", {"title": "t", "assignee": u["_guid"]})
        self.records.clear()
        # re-writing the identical assignee changes no edge → no touch
        objects.upsert_object("a", "task", t["_guid"],
                              {"assignee": u["_guid"]}, partial=True)
        self.assertEqual(self.records[0]["touched"], [])

    def test_delete_enumerates_neighbors(self):
        u = objects.create_object("a", "user", {"name": "ann"})
        t = objects.create_object("a", "task", {"title": "t", "assignee": u["_guid"]})
        self.records.clear()
        objects.delete_object("a", t["_guid"])
        self.assertIn(["user", u["_guid"]], self.records[0]["touched"])

    def test_slot_steal_touches_evicted_holder(self):
        # many_to_one: two tasks fight over one user's slot from the inverse side.
        u1 = objects.create_object("a", "user", {"name": "u1"})
        t = objects.create_object("a", "task", {"title": "t", "assignee": u1["_guid"]})
        u2 = objects.create_object("a", "user", {"name": "u2"})
        self.records.clear()
        # assign t to u2 by writing the inverse side (user.tasks=[t]); this steals
        # t out of u1's assignee slot — u1 must appear in touched.
        objects.upsert_object("a", "user", u2["_guid"], {"tasks": [t["_guid"]]},
                              partial=True)
        touched = self.records[0]["touched"]
        self.assertIn(["user", u1["_guid"]], touched)


class TestSchemaOps(SeamBase):
    def test_idempotent_reput_stages_nothing(self):
        # identical re-PUT of the existing type
        schema.upsert_type("a", "task", fields={"title": {"type": "string"},
                                                "done": {"type": "boolean"}})
        self.assertEqual(self.records, [])

    def test_field_change_stages_morph(self):
        schema.upsert_type("a", "task", merge=True,
                           fields={"priority": {"type": "number"}})
        self.assertEqual(len(self.records), 1)
        op = self.records[0]["schema_op"]
        self.assertEqual(op["op"], "morph")
        self.assertIn("task", op["affected_types"])

    def test_repoint_relation_names_old_endpoint(self):
        schema.upsert_type("a", "user", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "account", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "task", merge=True, relations={
            "owner": {"to": "user", "cardinality": "many_to_one",
                      "inverse": "tasks"}})
        self.records.clear()
        # repoint owner from user -> account; user loses its inverse relation
        schema.upsert_type("a", "task", merge=True, relations={
            "owner": {"to": "account", "cardinality": "many_to_one",
                      "inverse": "tasks"}})
        op = self.records[0]["schema_op"]
        self.assertIn("user", op["affected_types"])      # old endpoint
        self.assertIn("account", op["affected_types"])   # new endpoint

    def test_delete_type_names_endpoints(self):
        schema.upsert_type("a", "user", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "task", merge=True, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one",
                         "inverse": "tasks"}})
        self.records.clear()
        schema.delete_type("a", "task")
        op = self.records[0]["schema_op"]
        self.assertEqual(op["op"], "delete_type")
        self.assertEqual(set(op["affected_types"]), {"task", "user"})

    def test_delete_app_stages_delete_app(self):
        schema.delete_type("a", "task")  # clear noise
        self.records.clear()
        apps.delete_app("a")
        self.assertEqual(self.records[0]["schema_op"]["op"], "delete_app")


if __name__ == "__main__":
    unittest.main()
