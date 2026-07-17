"""The streams core module (PR2): registry, bus, workers, snapshot refresh,
coalescing, caps. Drives streams.attach() directly — no HTTP transport yet.
"""

import json
import time
import unittest

from morphdb import apps, db, objects, schema, streams


def parse(raw):
    """Decode one SSE frame's bytes into (event, data_obj, seq)."""
    event = data = seq = None
    for line in raw.decode().split("\n"):
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line.startswith("id: "):
            seq = int(line[4:])
    return event, data, seq


def drain(sub, timeout=1.5):
    """Next decoded (event, data, seq), skipping heartbeats. None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame, _term = sub.next_frame(0.2)
        if frame is streams.HEARTBEAT:
            continue
        if frame is streams.CLOSED:
            return ("_closed", None, None)
        return parse(frame)
    return None


class StreamBase(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")
        streams.reset()
        apps.register_app("a")
        schema.upsert_type("a", "task", fields={
            "title": {"type": "string"},
            "done": {"type": "boolean", "index": True, "default": False}})

    def tearDown(self):
        streams.stop()


class TestSnapshot(StreamBase):
    def test_attach_emits_init(self):
        objects.create_object("a", "task", {"title": "seed"})
        sub = streams.attach("a", "task")
        ev, data, seq = drain(sub)
        self.assertEqual(ev, "init")
        self.assertEqual(data["mode"], "snapshot")
        self.assertEqual(data["total"], 1)
        self.assertEqual(seq, 1)

    def test_write_triggers_snapshot(self):
        sub = streams.attach("a", "task", refresh="50")
        self.assertEqual(drain(sub)[0], "init")
        objects.create_object("a", "task", {"title": "new"})
        ev, data, seq = drain(sub)
        self.assertEqual(ev, "snapshot")
        self.assertEqual(data["total"], 1)
        self.assertEqual(seq, 2)

    def test_filter_scopes_the_stream(self):
        sub = streams.attach("a", "task", filters={"done": "true"}, refresh="50")
        self.assertEqual(drain(sub)[1]["total"], 0)
        objects.create_object("a", "task", {"title": "x", "done": False})
        # no-op for this query, but snapshot triggers coarsely → refresh with 0
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "snapshot")
        self.assertEqual(data["total"], 0)
        objects.create_object("a", "task", {"title": "y", "done": True})
        ev, data, _ = drain(sub)
        self.assertEqual(data["total"], 1)

    def test_debounce_coalesces_burst(self):
        sub = streams.attach("a", "task", refresh="300")
        self.assertEqual(drain(sub)[0], "init")
        # Warm last_run so the leading edge has already fired.
        objects.create_object("a", "task", {"title": "warm"})
        self.assertEqual(drain(sub, timeout=2.0)[1]["total"], 1)
        # Burst inside one debounce window → a single coalesced refresh.
        for i in range(3):
            objects.create_object("a", "task", {"title": f"t{i}"})
        ev, data, _ = drain(sub, timeout=2.0)
        self.assertEqual(ev, "snapshot")
        self.assertEqual(data["total"], 4)
        self.assertIsNone(drain(sub, timeout=0.6))   # no further snapshot


class TestCoalescing(StreamBase):
    def test_identical_queries_share_a_group(self):
        s1 = streams.attach("a", "task", refresh="50")
        s2 = streams.attach("a", "task", refresh="50")
        self.assertEqual(s1.qhash, s2.qhash)
        self.assertIs(s1.group, s2.group)
        self.assertEqual(len(s1.group.subs), 2)
        drain(s1); drain(s2)
        objects.create_object("a", "task", {"title": "z"})
        self.assertEqual(drain(s1)[0], "snapshot")
        self.assertEqual(drain(s2)[0], "snapshot")

    def test_equivalent_spellings_collide(self):
        s1 = streams.attach("a", "task", filters={"done": "false"})
        s2 = streams.attach("a", "task", filters={"done__eq": "false"})
        self.assertEqual(s1.qhash, s2.qhash)

    def test_different_apps_do_not_share(self):
        apps.register_app("b")
        schema.upsert_type("b", "task", fields={"title": {"type": "string"}})
        s1 = streams.attach("a", "task")
        s2 = streams.attach("b", "task")
        self.assertNotEqual(s1.qhash, s2.qhash)


class TestCaps(StreamBase):
    def test_app_cap_429s(self):
        import os
        os.environ["MORPHDB_STREAM_APP_CAP"] = "2"
        streams.reset()
        streams.attach("a", "task")
        streams.attach("a", "task", filters={"done": "true"})
        with self.assertRaises(Exception) as cm:
            streams.attach("a", "task", filters={"done": "false"})
        self.assertEqual(getattr(cm.exception, "status", None), 429)
        del os.environ["MORPHDB_STREAM_APP_CAP"]
        streams.reset()


class TestLifecycle(StreamBase):
    def test_detach_frees_slot(self):
        sub = streams.attach("a", "task")
        self.assertEqual(streams._TOTAL, 1)
        streams.detach(sub)
        self.assertEqual(streams._TOTAL, 0)
        self.assertEqual(streams._GROUPS, {})

    def test_interested_gate(self):
        self.assertFalse(streams.interested("a", ("task",)))
        sub = streams.attach("a", "task")
        self.assertTrue(streams.interested("a", ("task",)))
        self.assertFalse(streams.interested("a", ("user",)))
        streams.detach(sub)
        self.assertFalse(streams.interested("a", ("task",)))

    def test_type_delete_ends_stream(self):
        sub = streams.attach("a", "task", refresh="50")
        self.assertEqual(drain(sub)[0], "init")
        schema.delete_type("a", "task")
        ev, data, _ = drain(sub)
        self.assertEqual(ev, "end")
        self.assertEqual(data["error"]["code"], "type_deleted")
        self.assertEqual(drain(sub)[0], "_closed")

    def test_morph_rekey_does_not_corrupt_sibling_interest(self):
        # Two groups share trigger type 'tag' via a relation filter; a morph that
        # re-keys one group must not zero the interest count the other needs.
        schema.upsert_type("a", "tag", fields={"name": {"type": "string"}})
        schema.upsert_type("a", "task", merge=True, relations={
            "tag": {"to": "tag", "cardinality": "many_to_one", "inverse": "tasks"}})
        t = objects.create_object("a", "tag", {"name": "x"})
        g1 = streams.attach("a", "task", filters={"tag": t["_guid"]}, refresh="50")
        g2 = streams.attach("a", "task", filters={"tag__exists": "true"},
                            refresh="50")
        self.assertTrue(streams.interested("a", ("tag",)))
        # morph g1's streamed type; then detach g1
        schema.upsert_type("a", "task", merge=True,
                           fields={"p": {"type": "number", "index": True}})
        import time as _t; _t.sleep(0.3)
        streams.detach(g1)
        # g2 still lives and still triggers on 'tag' — interest must survive
        self.assertTrue(streams.interested("a", ("tag",)),
                        "sibling group's interest was corrupted by the re-key")

    def test_rollback_dirty_record_does_not_crash_dispatcher(self):
        sub = streams.attach("a", "task", refresh="50")
        self.assertEqual(drain(sub)[0], "init")
        # publish a synthetic rollback-heal record directly through the hook
        db._publish([{"app": "a", "dirty": [["a", "task"]]}])
        ev, _data, _ = drain(sub)
        self.assertEqual(ev, "snapshot")   # heal triggered a refresh, no crash


if __name__ == "__main__":
    unittest.main()
