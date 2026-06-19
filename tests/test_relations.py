"""Relations: declared once, read/written as fields on both sides."""

import unittest

from harness import Base


class TestDeclareAndProject(Base):
    def setUp(self):
        super().setUp()
        self.put_type("user", fields={"name": "string"})
        self.put_type("task", fields={"title": "string"}, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one", "inverse": "tasks"}})
        self.ann = self.create("user", {"name": "Ann"})["_guid"]

    def test_inverse_appears_on_other_type(self):
        st, b, _ = self.get("/schema/user")
        self.assertIn("tasks", b["inverse_relations"])
        self.assertEqual(b["inverse_relations"]["tasks"]["via_relation"], "assignee")

    def test_to_one_scalar_to_many_list(self):
        t = self.create("task", {"title": "x", "assignee": self.ann})
        self.assertEqual(t["assignee"], self.ann)                 # scalar
        self.assertEqual(self.read("user", self.ann)["tasks"], [t["_guid"]])  # list

    def test_relation_present_in_list_reads(self):
        self.create("task", {"title": "x", "assignee": self.ann})
        st, b, _ = self.get("/objects/task")
        self.assertEqual(b["objects"][0]["assignee"], self.ann)
        st, b, _ = self.get("/objects/user")
        self.assertEqual(len(b["objects"][0]["tasks"]), 1)

    def test_write_from_either_side(self):
        t = self.create("task", {"title": "x"})
        # set from the user (to-many) side
        self.patch(f"/objects/user/{self.ann}", {"tasks": [t["_guid"]]})
        self.assertEqual(self.read("task", t["_guid"])["assignee"], self.ann)
        # clear from the task (to-one) side
        self.patch(f"/objects/task/{t['_guid']}", {"assignee": None})
        self.assertEqual(self.read("user", self.ann)["tasks"], [])

    def test_last_write_wins_steal(self):
        bob = self.create("user", {"name": "Bob"})["_guid"]
        t = self.create("task", {"title": "x", "assignee": self.ann})
        # reassigning the same task to Bob steals it from Ann (many_to_one)
        self.patch(f"/objects/task/{t['_guid']}", {"assignee": bob})
        self.assertEqual(self.read("user", self.ann)["tasks"], [])
        self.assertEqual(self.read("user", bob)["tasks"], [t["_guid"]])

    def test_bad_target_type_rejected(self):
        other = self.create("task", {"title": "y"})["_guid"]
        st, b, _ = self.post("/objects/task", {"title": "x", "assignee": other})
        self.assertEqual(st, 400)               # a task is not a user
        self.assertIn("user", b["error"]["message"])

    def test_missing_target_rejected(self):
        st, _, _ = self.post("/objects/task", {"title": "x", "assignee": "user_ghost"})
        self.assertEqual(st, 400)

    def test_to_one_rejects_list(self):
        st, _, _ = self.post("/objects/task", {"title": "x", "assignee": [self.ann]})
        self.assertEqual(st, 400)

    def test_delete_object_keeps_neighbor(self):
        t = self.create("task", {"title": "x", "assignee": self.ann})
        self.delete(f"/objects/task/{t['_guid']}")
        st, _, _ = self.get(f"/objects/user/{self.ann}")
        self.assertEqual(st, 200)               # neighbor user survives
        self.assertEqual(self.read("user", self.ann)["tasks"], [])


class TestCardinalities(Base):
    def setUp(self):
        super().setUp()
        self.put_type("u", fields={"n": "string"})
        self.a = self.create("u", {"n": "a"})["_guid"]
        self.b = self.create("u", {"n": "b"})["_guid"]

    def test_many_to_many_both_lists(self):
        self.put_type("tag", fields={"label": "string"})
        self.put_type("post", fields={"title": "string"}, relations={
            "tags": {"to": "tag", "cardinality": "many_to_many", "inverse": "posts"}})
        t1 = self.create("tag", {"label": "x"})["_guid"]
        t2 = self.create("tag", {"label": "y"})["_guid"]
        p = self.create("post", {"title": "p", "tags": [t1, t2]})
        self.assertEqual(sorted(p["tags"]), sorted([t1, t2]))
        self.assertEqual(self.read("tag", t1)["posts"], [p["_guid"]])

    def test_one_to_one_steal(self):
        self.put_type("u", merge=True, relations={
            "spouse": {"to": "u", "cardinality": "one_to_one", "inverse": "spouse_of"}})
        c = self.create("u", {"n": "c"})["_guid"]
        self.patch(f"/objects/u/{self.a}", {"spouse": self.b})
        # a<->b set; now c marries b, stealing b from a
        self.patch(f"/objects/u/{c}", {"spouse": self.b})
        self.assertIsNone(self.read("u", self.a)["spouse"])
        self.assertEqual(self.read("u", c)["spouse"], self.b)


class TestSymmetric(Base):
    def setUp(self):
        super().setUp()
        self.put_type("u", fields={"n": "string"}, relations={
            "friends": {"to": "u", "cardinality": "many_to_many", "symmetric": True}})
        self.a = self.create("u", {"n": "a"})["_guid"]
        self.b = self.create("u", {"n": "b"})["_guid"]

    def test_mutual_and_counted_once(self):
        self.patch(f"/objects/u/{self.a}", {"friends": [self.b]})
        # appears on both ends under one label
        self.assertEqual(self.read("u", self.a)["friends"], [self.b])
        self.assertEqual(self.read("u", self.b)["friends"], [self.a])
        # setting the reverse is idempotent (same edge), still one each
        self.patch(f"/objects/u/{self.b}", {"friends": [self.a]})
        self.assertEqual(self.read("u", self.a)["friends"], [self.b])

    def test_symmetric_requires_same_type(self):
        self.put_type("v", fields={"x": "string"})
        st, _, _ = self.put("/schema/u", {"merge": True, "relations": {
            "bad": {"to": "v", "cardinality": "many_to_many", "symmetric": True}}})
        self.assertEqual(st, 400)


class TestSchemaValidation(Base):
    def setUp(self):
        super().setUp()
        self.put_type("user", fields={"name": "string"})

    def test_relation_field_name_collision_rejected(self):
        st, _, _ = self.put("/schema/task", {
            "fields": {"assignee": "string"},
            "relations": {"assignee": {"to": "user", "cardinality": "many_to_one",
                                       "inverse": "tasks"}}})
        self.assertEqual(st, 400)

    def test_self_relation_identical_names_rejected(self):
        st, _, _ = self.put("/schema/user", {"merge": True, "relations": {
            "peer": {"to": "user", "cardinality": "many_to_many", "inverse": "peer"}}})
        self.assertEqual(st, 400)        # steer to symmetric:true

    def test_relation_to_unknown_type_rejected(self):
        st, _, _ = self.put("/schema/task", {"relations": {
            "x": {"to": "ghost", "cardinality": "many_to_one", "inverse": "y"}}})
        self.assertEqual(st, 400)

    def test_drop_relation_removes_edges_and_inverse(self):
        self.put_type("task", fields={"title": "string"}, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one", "inverse": "tasks"}})
        u = self.create("user", {"name": "Ann"})["_guid"]
        self.create("task", {"title": "x", "assignee": u})
        # replace relations with empty set -> drops assignee + its edges
        self.put("/schema/task", {"relations": {}})
        st, b, _ = self.get("/schema/user")
        self.assertNotIn("tasks", b["inverse_relations"])
        self.assertEqual(self.read("user", u)["tasks"] if "tasks" in self.read("user", u) else [], [])

    def test_delete_type_removes_inverse_from_other(self):
        self.put_type("task", fields={"title": "string"}, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one", "inverse": "tasks"}})
        u = self.create("user", {"name": "Ann"})["_guid"]
        self.create("task", {"title": "x", "assignee": u})
        self.delete("/schema/task")
        st, b, _ = self.get("/schema/user")
        self.assertEqual(b["inverse_relations"], {})       # tasks gone
        st, _, _ = self.get(f"/objects/user/{u}")
        self.assertEqual(st, 200)                          # user survives


if __name__ == "__main__":
    unittest.main(verbosity=2)
