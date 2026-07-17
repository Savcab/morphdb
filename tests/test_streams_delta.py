"""Delta mode (PR4): enter/update/leave membership events, eligibility 400s,
the matcher seed, relation both-ends touches, and morph re-seed.

Drives streams.attach(mode="delta") directly; reuses the SSE frame decoder.
"""

import unittest

from morphdb import apps, db, objects, schema, streams
from tests.test_streams_core import drain, StreamBase


class DeltaBase(StreamBase):
    def setUp(self):
        super().setUp()
        schema.upsert_type("a", "user", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "task", merge=True, relations={
            "assignee": {"to": "user", "cardinality": "many_to_one",
                         "inverse": "tasks"}})


class TestDeltaMembership(DeltaBase):
    def test_init_is_unpaginated_set(self):
        for i in range(3):
            objects.create_object("a", "task", {"title": f"t{i}", "done": False})
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        ev, data, seq = drain(sub)
        self.assertEqual(ev, "init")
        self.assertEqual(data["mode"], "delta")
        self.assertEqual(data["total"], 3)
        self.assertEqual(seq, 1)

    def test_enter_on_new_match(self):
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 0)
        obj = objects.create_object("a", "task", {"title": "x", "done": False})
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "enter")
        self.assertEqual(data["object"]["_guid"], obj["_guid"])

    def test_update_then_leave(self):
        obj = objects.create_object("a", "task", {"title": "x", "done": False})
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 1)
        # still matches → update
        objects.upsert_object("a", "task", obj["_guid"], {"title": "y"},
                              partial=True)
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "update")
        self.assertEqual(data["object"]["title"], "y")
        # no longer matches → leave
        objects.upsert_object("a", "task", obj["_guid"], {"done": True},
                              partial=True)
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "leave")
        self.assertEqual(data["guid"], obj["_guid"])

    def test_delete_is_leave(self):
        obj = objects.create_object("a", "task", {"title": "x", "done": False})
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        drain(sub)
        objects.delete_object("a", obj["_guid"])
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "leave")
        self.assertEqual(data["guid"], obj["_guid"])

    def test_no_op_write_is_silent(self):
        obj = objects.create_object("a", "task", {"title": "x", "done": True})
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 0)
        # a write that never matches produces no event
        objects.upsert_object("a", "task", obj["_guid"], {"title": "z"},
                              partial=True)
        self.assertIsNone(drain(sub, timeout=0.6))


class TestDeltaRelations(DeltaBase):
    def test_relation_filter_both_ends(self):
        u = objects.create_object("a", "user", {"name": "ann"})
        # stream: tasks assigned to ann
        sub = streams.attach("a", "task", filters={"assignee": u["_guid"]},
                             mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 0)
        # a task WRITE names the user as assignee → the task enters
        t = objects.create_object("a", "task", {"title": "t",
                                                "assignee": u["_guid"]})
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "enter")
        self.assertEqual(data["object"]["_guid"], t["_guid"])

    def test_inverse_side_stream_sees_neighbor_write(self):
        # stream over users that have any task (relation-exists on the inverse)
        u = objects.create_object("a", "user", {"name": "ann"})
        sub = streams.attach("a", "user", filters={"tasks__exists": "true"},
                             mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 0)
        # writing a task pointed at ann changes ann's projected body (she now
        # has a task) though ann herself was never written → she enters
        objects.create_object("a", "task", {"title": "t", "assignee": u["_guid"]})
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "enter")
        self.assertEqual(data["object"]["_guid"], u["_guid"])


class TestDeltaEligibility(DeltaBase):
    def _expect_400(self, **kw):
        with self.assertRaises(Exception) as cm:
            streams.attach("a", "task", mode="delta", **kw)
        self.assertEqual(getattr(cm.exception, "status", None), 400)
        return str(cm.exception)

    def test_include_rejected(self):
        self.assertIn("include", self._expect_400(include="assignee"))

    def test_limit_rejected(self):
        self._expect_400(limit=10)

    def test_offset_rejected(self):
        self._expect_400(offset=5)

    def test_refresh_rejected(self):
        self.assertIn("refresh", self._expect_400(refresh="100"))

    def test_bad_mode_is_didactic(self):
        with self.assertRaises(Exception) as cm:
            streams.attach("a", "task", mode="urgent")
        self.assertEqual(getattr(cm.exception, "status", None), 400)
        self.assertIn("reserved", str(cm.exception))


class TestDeltaCoalescing(DeltaBase):
    def test_shared_membership_one_eval_fans_out(self):
        s1 = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        s2 = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(s1.qhash, s2.qhash)
        self.assertIs(s1.group.state, s2.group.state)
        drain(s1); drain(s2)
        objects.create_object("a", "task", {"title": "x", "done": False})
        self.assertEqual(drain(s1)[0], "enter")
        self.assertEqual(drain(s2)[0], "enter")

    def test_delta_drops_sort_from_hash(self):
        s1 = streams.attach("a", "task", filters={"done": "false"},
                            sort="title", mode="delta")
        s2 = streams.attach("a", "task", filters={"done": "false"},
                            mode="delta")
        self.assertEqual(s1.qhash, s2.qhash)   # ordering is client-side in delta


class TestDeltaEligibilityEdges(DeltaBase):
    def test_explicit_offset_zero_is_allowed(self):
        # offset=0 is not a window; the HTTP path passes "0" as a string
        sub = streams.attach("a", "task", offset="0", mode="delta")
        self.assertEqual(drain(sub)[0], "init")

    def test_explicit_offset_zero_int_allowed(self):
        sub = streams.attach("a", "task", offset=0, mode="delta")
        self.assertEqual(drain(sub)[0], "init")


class TestDeltaMorph(DeltaBase):
    def test_morph_reseeds_to_init(self):
        objects.create_object("a", "task", {"title": "x", "done": False})
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(drain(sub)[0], "init")
        # add an indexed field — a schema morph on the streamed type
        schema.upsert_type("a", "task", merge=True,
                           fields={"priority": {"type": "number", "index": True}})
        ev, data, _ = drain(sub, timeout=2.0)
        self.assertEqual(ev, "init")           # collapse to fresh init
        self.assertEqual(data["mode"], "delta")

    def test_morph_reseed_reflects_post_morph_writes(self):
        # A write that lands around a morph must not be lost: whether it folds
        # into the re-seed init or arrives as a later enter, the final membership
        # reconstructed from events must include it.
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        self.assertEqual(drain(sub)[1]["total"], 0)
        schema.upsert_type("a", "task", merge=True,
                           fields={"priority": {"type": "number", "index": True}})
        objects.create_object("a", "task", {"title": "new", "done": False})
        members = set()
        for _ in range(20):
            f = drain(sub, timeout=0.5)
            if f is None:
                break
            ev, data, _s = f
            if ev == "init":
                members = {o["_guid"] for o in data["objects"]}
            elif ev == "enter":
                members.add(data["object"]["_guid"])
            elif ev == "leave":
                members.discard(data["guid"])
        self.assertEqual(len(members), 1)


class TestDeltaReseedLostUpdate(DeltaBase):
    def test_no_lost_enter_across_reseed(self):
        # A membership set that re-seeds while writes flow must not lose a member.
        sub = streams.attach("a", "task", filters={"done": "false"}, mode="delta")
        drain(sub)
        for i in range(5):
            objects.create_object("a", "task", {"title": f"t{i}", "done": False})
        # force a re-seed via schema morph, then verify final membership is exact
        schema.upsert_type("a", "task", merge=True,
                           fields={"note": {"type": "string"}})
        import time as _t; _t.sleep(0.5)
        # drain everything; reconstruct the set from events
        members = set()
        for _ in range(40):
            f = drain(sub, timeout=0.4)
            if f is None:
                break
            ev, data, _s = f
            if ev == "init":
                members = {o["_guid"] for o in data["objects"]}
            elif ev == "enter":
                members.add(data["object"]["_guid"])
            elif ev == "leave":
                members.discard(data["guid"])
        st = sub.group.state
        self.assertEqual(members, set(st.members))
        self.assertEqual(len(members), 5)


if __name__ == "__main__":
    unittest.main()
