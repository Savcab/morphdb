"""Relations: declared once, read/written as fields on both sides."""

import unittest

from harness import APP, Base


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


class TestRelationFiltering(Base):
    """Relations are filterable on the list endpoint through the indexed
    ``associations`` table: ``?rel=<guid>`` (eq), plus ``__in`` / ``__ne`` /
    ``__exists``. This is the awb "filter children by their parent" case that
    previously forced agents to model the link as an un-indexed FK-as-field.
    """

    def setUp(self):
        super().setUp()
        # The symmetric self-relation is declared in the same call as the type;
        # that works because the type row is written before its relations.
        self.put_type("user", fields={"name": "string"}, relations={
            "friends": {"to": "user", "cardinality": "many_to_many",
                        "symmetric": True}})
        self.put_type("tag", fields={"label": "string"})
        self.put_type("task",
            fields={"title": "string", "done": "boolean",
                    "priority": {"type": "number", "index": True}},
            relations={
                "assignee": {"to": "user", "cardinality": "many_to_one",
                             "inverse": "tasks"},
                "tags": {"to": "tag", "cardinality": "many_to_many",
                         "inverse": "tagged"}})
        self.ann = self.create("user", {"name": "Ann"})["_guid"]
        self.bob = self.create("user", {"name": "Bob"})["_guid"]
        self.urgent = self.create("tag", {"label": "urgent"})["_guid"]
        self.t1 = self.create("task", {"title": "a1", "assignee": self.ann,
                                       "done": True, "priority": 1,
                                       "tags": [self.urgent]})["_guid"]
        self.t2 = self.create("task", {"title": "a2", "assignee": self.ann,
                                       "done": False, "priority": 2})["_guid"]
        self.t3 = self.create("task", {"title": "b1", "assignee": self.bob,
                                       "done": False, "priority": 3})["_guid"]
        self.t4 = self.create("task", {"title": "free", "done": False,
                                       "priority": 4})["_guid"]

    def _guids(self, path):
        st, b, _ = self.get(path)
        self.assertEqual(st, 200, b)
        return {o["_guid"] for o in b["objects"]}, b["total"]

    # --- the core case: filter a type's list by a relation (forward side) ------
    def test_eq_forward_many_to_one(self):
        g, total = self._guids(f"/objects/task?assignee={self.ann}")
        self.assertEqual(g, {self.t1, self.t2})
        self.assertEqual(total, 2)

    def test_eq_other_neighbor(self):
        g, total = self._guids(f"/objects/task?assignee={self.bob}")
        self.assertEqual((g, total), ({self.t3}, 1))

    # --- relation filter composes with a field filter (the real awb query) -----
    def test_relation_and_field_filter_compose(self):
        # Ann's tasks with priority >= 2 -> just t2 (t1 is priority 1)
        g, total = self._guids(f"/objects/task?assignee={self.ann}&priority__gte=2")
        self.assertEqual((g, total), ({self.t2}, 1))

    # --- inverse side ----------------------------------------------------------
    def test_eq_inverse_side(self):
        # users whose `tasks` include t3 -> Bob
        g, _ = self._guids(f"/objects/user?tasks={self.t3}")
        self.assertEqual(g, {self.bob})

    # --- many_to_many, both directions -----------------------------------------
    def test_eq_many_to_many(self):
        g, _ = self._guids(f"/objects/task?tags={self.urgent}")
        self.assertEqual(g, {self.t1})
        g2, _ = self._guids(f"/objects/tag?tagged={self.t1}")
        self.assertEqual(g2, {self.urgent})

    # --- exists ----------------------------------------------------------------
    def test_exists_true_false(self):
        assigned, _ = self._guids("/objects/task?assignee__exists=true")
        self.assertEqual(assigned, {self.t1, self.t2, self.t3})
        unassigned, total = self._guids("/objects/task?assignee__exists=false")
        self.assertEqual((unassigned, total), ({self.t4}, 1))

    # --- in --------------------------------------------------------------------
    def test_in_multiple_neighbors(self):
        g, _ = self._guids(
            f"/objects/task?assignee__in={self.ann},{self.bob}")
        self.assertEqual(g, {self.t1, self.t2, self.t3})

    # --- ne (note: "not assigned to Ann" includes the unassigned task) ---------
    def test_ne_excludes_neighbor(self):
        g, _ = self._guids(f"/objects/task?assignee__ne={self.ann}")
        self.assertEqual(g, {self.t3, self.t4})

    # --- symmetric: one canonical edge must filter from either end -------------
    def test_symmetric_either_direction(self):
        self.patch(f"/objects/user/{self.ann}", {"friends": [self.bob]})
        a, _ = self._guids(f"/objects/user?friends={self.bob}")
        self.assertEqual(a, {self.ann})
        b, _ = self._guids(f"/objects/user?friends={self.ann}")
        self.assertEqual(b, {self.bob})

    # --- sort + pagination still apply on top of a relation filter -------------
    def test_relation_filter_with_sort_and_pagination(self):
        st, b, _ = self.get(
            f"/objects/task?assignee={self.ann}&sort=priority&order=desc&limit=1")
        self.assertEqual(st, 200, b)
        self.assertEqual(b["total"], 2)              # counts the full filtered set
        self.assertEqual(len(b["objects"]), 1)       # one page
        self.assertEqual(b["objects"][0]["_guid"], self.t2)   # priority 2 > 1, desc

    # --- errors ----------------------------------------------------------------
    def test_unsupported_op_on_relation_rejected(self):
        st, b, _ = self.get(f"/objects/task?assignee__gt={self.ann}")
        self.assertEqual(st, 400)
        self.assertIn("assignee", b["error"]["message"])

    def test_unknown_key_message_mentions_relations(self):
        st, b, _ = self.get("/objects/task?nope=x")
        self.assertEqual(st, 400)
        msg = b["error"]["message"]
        self.assertIn("relations", msg)              # no longer "fields, not relations"
        self.assertIn("assignee", msg)               # lists the available relations

    # --- the headline claim: a relation filter is index-backed -----------------
    def test_relation_filter_is_index_backed(self):
        from morphdb import db
        plan = db.conn().execute(
            "EXPLAIN QUERY PLAN SELECT from_guid FROM associations "
            "WHERE app=? AND assoc_name=? AND to_guid=?",
            (APP, "task__assignee", self.ann),
        ).fetchall()
        text = " | ".join(r["detail"] for r in plan)
        # An index SEARCH (any of the associations indexes, incl. the covering
        # UNIQUE index), never a full table SCAN — this is the whole point: a
        # relation filter is index-backed, where a field filter scans the blob.
        self.assertIn("SEARCH associations USING", text)
        self.assertIn("INDEX", text)
        self.assertNotIn("SCAN associations", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
