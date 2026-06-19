"""End-to-end test suite for MorphDB.

Uses only the standard library (``unittest`` + ``urllib``) so it runs anywhere
Python does, with no install step. A single server runs in a background thread;
each test resets the in-memory database in ``setUp`` for isolation.

Run:  python -m unittest discover -s tests   (from the repo root)
  or: python tests/test_morphdb.py
"""

import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morphdb import db                      # noqa: E402
from morphdb.server import Handler, MorphServer  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_PORT = _free_port()
_BASE = f"http://127.0.0.1:{_PORT}"
_HTTPD = None


def setUpModule():
    global _HTTPD
    os.environ["MORPHDB_QUIET"] = "1"
    db.init_db(":memory:")
    _HTTPD = MorphServer(("127.0.0.1", _PORT), Handler)
    t = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    t.start()


def tearDownModule():
    if _HTTPD is not None:
        _HTTPD.shutdown()
        _HTTPD.server_close()


def req(method, path, body=None, headers=None):
    url = _BASE + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None), resp
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e


class Base(unittest.TestCase):
    def setUp(self):
        db.init_db(":memory:")  # fresh schema per test

    # convenience wrappers
    def get(self, p):
        return req("GET", p)

    def post(self, p, b=None):
        return req("POST", p, b)

    def put(self, p, b=None):
        return req("PUT", p, b)

    def patch(self, p, b=None):
        return req("PATCH", p, b)

    def delete(self, p, b=None):
        return req("DELETE", p, b)

    def mk_task_schema(self):
        st, body, _ = self.post("/schemas/objects", {
            "name": "task",
            "fields": {"title": "string", "done": "boolean",
                       "priority": "number", "meta": "json"},
        })
        self.assertEqual(st, 201, body)
        return body


class TestMeta(Base):
    def test_health(self):
        st, b, _ = self.get("/health")
        self.assertEqual(st, 200)
        self.assertEqual(b["status"], "ok")

    def test_root_and_help(self):
        st, b, _ = self.get("/")
        self.assertEqual(st, 200)
        self.assertEqual(b["name"], "MorphDB")
        st, b, _ = self.get("/help")
        self.assertEqual(st, 200)
        self.assertIn("endpoints", b)

    def test_404_and_405(self):
        st, b, _ = self.get("/nope")
        self.assertEqual(st, 404)
        self.assertEqual(b["error"]["code"], "not_found")
        st, b, _ = self.delete("/health")
        self.assertEqual(st, 405)

    def test_cors_preflight(self):
        st, _, resp = req("OPTIONS", "/objects/task")
        self.assertEqual(st, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")


class TestObjectSchema(Base):
    def test_create_and_get(self):
        self.mk_task_schema()
        st, b, _ = self.get("/schemas/objects/task")
        self.assertEqual(st, 200)
        self.assertEqual(b["fields"]["title"]["type"], "string")

    def test_list(self):
        self.mk_task_schema()
        st, b, _ = self.get("/schemas/objects")
        self.assertEqual(len(b["schemas"]), 1)

    def test_bad_type_rejected(self):
        st, b, _ = self.post("/schemas/objects",
                             {"name": "x", "fields": {"a": "stringg"}})
        self.assertEqual(st, 400)

    def test_reserved_field_rejected(self):
        st, b, _ = self.post("/schemas/objects",
                             {"name": "x", "fields": {"_guid": "string"}})
        self.assertEqual(st, 400)

    def test_bad_name_rejected(self):
        st, _, _ = self.post("/schemas/objects",
                             {"name": "9bad", "fields": {"a": "string"}})
        self.assertEqual(st, 400)

    def test_merge_adds_field(self):
        self.mk_task_schema()
        st, b, _ = self.put("/schemas/objects/task",
                            {"merge": True, "fields": {"tags": "json"}})
        self.assertEqual(st, 200)
        self.assertIn("tags", b["fields"])
        self.assertIn("title", b["fields"])  # original preserved

    def test_replace_drops_field(self):
        self.mk_task_schema()
        st, b, _ = self.put("/schemas/objects/task", {"fields": {"only": "string"}})
        self.assertEqual(st, 200)
        self.assertEqual(list(b["fields"].keys()), ["only"])

    def test_delete_fields(self):
        self.mk_task_schema()
        st, b, _ = self.post("/schemas/objects/task/delete-fields",
                             {"fields": ["meta"]})
        self.assertEqual(st, 200)
        self.assertNotIn("meta", b["fields"])


class TestObjectCRUD(Base):
    def setUp(self):
        super().setUp()
        self.mk_task_schema()

    def test_create_read(self):
        st, b, _ = self.post("/objects/task",
                             {"title": "a", "done": False, "priority": 1})
        self.assertEqual(st, 201)
        guid = b["_guid"]
        st, b2, _ = self.get(f"/objects/task/{guid}")
        self.assertEqual(st, 200)
        self.assertEqual(b2["title"], "a")
        # by guid alone
        st, b3, _ = self.get(f"/object/{guid}")
        self.assertEqual(b3["_guid"], guid)

    def test_unknown_field_rejected(self):
        st, b, _ = self.post("/objects/task", {"title": "a", "bogus": 1})
        self.assertEqual(st, 400)

    def test_type_coercion(self):
        st, b, _ = self.post("/objects/task",
                             {"title": 123, "done": "yes", "priority": "4"})
        self.assertEqual(st, 201)
        self.assertEqual(b["title"], "123")
        self.assertIs(b["done"], True)
        self.assertEqual(b["priority"], 4)

    def test_boolean_for_number_rejected(self):
        st, _, _ = self.post("/objects/task", {"priority": True})
        self.assertEqual(st, 400)

    def test_patch_merges(self):
        st, b, _ = self.post("/objects/task", {"title": "a", "priority": 1})
        guid = b["_guid"]
        st, b2, _ = self.patch(f"/objects/task/{guid}", {"priority": 9})
        self.assertEqual(b2["title"], "a")
        self.assertEqual(b2["priority"], 9)

    def test_put_replaces(self):
        st, b, _ = self.post("/objects/task", {"title": "a", "priority": 1})
        guid = b["_guid"]
        st, b2, _ = self.put(f"/objects/task/{guid}", {"title": "b"})
        self.assertEqual(b2["title"], "b")
        self.assertIsNone(b2["priority"])  # replaced -> default null

    def test_delete(self):
        st, b, _ = self.post("/objects/task", {"title": "a"})
        guid = b["_guid"]
        st, _, _ = self.delete(f"/objects/task/{guid}")
        self.assertEqual(st, 200)
        st, _, _ = self.get(f"/objects/task/{guid}")
        self.assertEqual(st, 404)

    def test_wrong_type_path_404(self):
        st, b, _ = self.post("/objects/task", {"title": "a"})
        guid = b["_guid"]
        self.post("/schemas/objects", {"name": "user", "fields": {"n": "string"}})
        st, _, _ = self.get(f"/objects/user/{guid}")
        self.assertEqual(st, 404)

    def test_create_on_missing_type(self):
        st, _, _ = self.post("/objects/ghost", {"x": 1})
        self.assertEqual(st, 404)


class TestLazyInvalidation(Base):
    def setUp(self):
        super().setUp()
        self.mk_task_schema()

    def test_dropped_field_hidden_added_field_null(self):
        st, b, _ = self.post("/objects/task",
                             {"title": "a", "meta": {"k": 1}, "priority": 3})
        guid = b["_guid"]
        # drop meta, add tags — without touching the stored row
        self.post("/schemas/objects/task/delete-fields", {"fields": ["meta"]})
        self.put("/schemas/objects/task",
                 {"merge": True, "fields": {"tags": "json"}})
        st, b2, _ = self.get(f"/objects/task/{guid}")
        self.assertNotIn("meta", b2)       # gone
        self.assertIn("tags", b2)          # appears
        self.assertIsNone(b2["tags"])      # as null
        self.assertEqual(b2["priority"], 3)  # untouched data survives

    def test_readd_field_recovers_value(self):
        # lazy invalidation keeps the underlying blob, so re-adding a dropped
        # field brings the old value back.
        st, b, _ = self.post("/objects/task", {"title": "a", "priority": 7})
        guid = b["_guid"]
        self.post("/schemas/objects/task/delete-fields", {"fields": ["priority"]})
        st, b2, _ = self.get(f"/objects/task/{guid}")
        self.assertNotIn("priority", b2)
        self.put("/schemas/objects/task",
                 {"merge": True, "fields": {"priority": "number"}})
        st, b3, _ = self.get(f"/objects/task/{guid}")
        self.assertEqual(b3["priority"], 7)


class TestQuery(Base):
    def setUp(self):
        super().setUp()
        self.mk_task_schema()
        for t, p, d in [("a", 1, True), ("b", 3, False),
                        ("c", 5, False), ("d", 5, True)]:
            self.post("/objects/task", {"title": t, "priority": p, "done": d})

    def n(self, path):
        st, b, _ = self.get(path)
        self.assertEqual(st, 200, b)
        return b

    def test_eq(self):
        self.assertEqual(self.n("/objects/task?done=true")["total"], 2)

    def test_gte_lt(self):
        self.assertEqual(self.n("/objects/task?priority__gte=3")["total"], 3)
        self.assertEqual(self.n("/objects/task?priority__lt=5")["total"], 2)

    def test_ne(self):
        self.assertEqual(self.n("/objects/task?priority__ne=5")["total"], 2)

    def test_in(self):
        self.assertEqual(self.n("/objects/task?priority__in=1,5")["total"], 3)

    def test_contains(self):
        self.assertEqual(self.n("/objects/task?title__contains=b")["total"], 1)

    def test_sort_and_pagination(self):
        b = self.n("/objects/task?sort=priority&order=desc&limit=2")
        self.assertEqual(b["total"], 4)
        self.assertEqual(len(b["objects"]), 2)
        self.assertEqual(b["objects"][0]["priority"], 5)
        b2 = self.n("/objects/task?sort=priority&order=desc&limit=2&offset=2")
        self.assertEqual(b2["objects"][0]["priority"], 3)

    def test_filter_unknown_field(self):
        st, _, _ = self.get("/objects/task?nope=1")
        self.assertEqual(st, 400)

    def test_exists(self):
        # all rows have priority set, so exists=false -> 0
        self.assertEqual(self.n("/objects/task?priority__exists=false")["total"], 0)


class TestAssociations(Base):
    def setUp(self):
        super().setUp()
        self.post("/schemas/objects", {"name": "user", "fields": {"name": "string"}})
        self.post("/schemas/objects", {"name": "task", "fields": {"title": "string"}})

    def mk(self, type_, **fields):
        st, b, _ = self.post(f"/objects/{type_}", fields)
        self.assertEqual(st, 201, b)
        return b["_guid"]

    def assoc_schema(self, name, card, ft="user", tt="task",
                     fwd="tasks", inv="owner"):
        st, b, _ = self.post("/schemas/associations", {
            "name": name, "from_type": ft, "to_type": tt,
            "forward_name": fwd, "inverse_name": inv, "cardinality": card,
        })
        self.assertEqual(st, 201, b)

    def test_create_and_traverse(self):
        self.assoc_schema("owns", "one_to_many")
        u = self.mk("user", name="alice")
        t = self.mk("task", title="x")
        st, b, _ = self.post("/associations",
                             {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        self.assertEqual(st, 201, b)
        # forward from user
        st, b, _ = self.get(f"/object/{u}/associations?relation=tasks&expand=true")
        self.assertEqual(b["total"], 1)
        self.assertEqual(b["associations"][0]["neighbor"]["title"], "x")
        # inverse from task
        st, b, _ = self.get(f"/object/{t}/associations?relation=owner")
        self.assertEqual(b["associations"][0]["neighbor_guid"], u)

    def test_idempotent(self):
        self.assoc_schema("owns", "many_to_many")
        u = self.mk("user", name="a")
        t = self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        st, b, _ = self.get(f"/object/{u}/associations")
        self.assertEqual(b["total"], 1)

    def test_self_loop_rejected(self):
        self.assoc_schema("friend", "many_to_many", ft="user", tt="user",
                          fwd="friends", inv="friends")
        u = self.mk("user", name="a")
        st, _, _ = self.post("/associations",
                             {"assoc_name": "friend", "from_guid": u, "to_guid": u})
        self.assertEqual(st, 400)

    def test_type_mismatch_rejected(self):
        self.assoc_schema("owns", "one_to_many")
        u = self.mk("user", name="a")
        u2 = self.mk("user", name="b")
        st, _, _ = self.post("/associations",
                             {"assoc_name": "owns", "from_guid": u, "to_guid": u2})
        self.assertEqual(st, 400)

    def test_one_to_one(self):
        self.assoc_schema("badge", "one_to_one")
        u = self.mk("user", name="a")
        t1 = self.mk("task", title="x")
        t2 = self.mk("task", title="y")
        self.post("/associations", {"assoc_name": "badge", "from_guid": u, "to_guid": t1})
        # user already has one -> conflict
        st, _, _ = self.post("/associations",
                             {"assoc_name": "badge", "from_guid": u, "to_guid": t2})
        self.assertEqual(st, 409)

    def test_one_to_many(self):
        self.assoc_schema("owns", "one_to_many")
        u1 = self.mk("user", name="a")
        u2 = self.mk("user", name="b")
        t = self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u1, "to_guid": t})
        # task can only have one owner -> second user conflicts
        st, _, _ = self.post("/associations",
                             {"assoc_name": "owns", "from_guid": u2, "to_guid": t})
        self.assertEqual(st, 409)
        # but one user can own many tasks
        t2 = self.mk("task", title="y")
        st, _, _ = self.post("/associations",
                             {"assoc_name": "owns", "from_guid": u1, "to_guid": t2})
        self.assertEqual(st, 201)

    def test_many_to_one(self):
        self.assoc_schema("belongs", "many_to_one", ft="task", tt="user",
                          fwd="owner", inv="tasks")
        t1 = self.mk("task", title="x")
        t2 = self.mk("task", title="y")
        u1 = self.mk("user", name="a")
        u2 = self.mk("user", name="b")
        self.post("/associations", {"assoc_name": "belongs", "from_guid": t1, "to_guid": u1})
        # task can only belong to one user
        st, _, _ = self.post("/associations",
                             {"assoc_name": "belongs", "from_guid": t1, "to_guid": u2})
        self.assertEqual(st, 409)
        # many tasks -> one user is fine
        st, _, _ = self.post("/associations",
                             {"assoc_name": "belongs", "from_guid": t2, "to_guid": u1})
        self.assertEqual(st, 201)

    def test_many_to_many(self):
        self.assoc_schema("tag", "many_to_many")
        u1, u2 = self.mk("user", name="a"), self.mk("user", name="b")
        t1, t2 = self.mk("task", title="x"), self.mk("task", title="y")
        for a in (u1, u2):
            for b in (t1, t2):
                st, _, _ = self.post("/associations",
                                     {"assoc_name": "tag", "from_guid": a, "to_guid": b})
                self.assertEqual(st, 201)
        st, b, _ = self.get(f"/object/{u1}/associations")
        self.assertEqual(b["total"], 2)

    def test_replace(self):
        self.assoc_schema("owns", "one_to_many")
        u1, u2 = self.mk("user", name="a"), self.mk("user", name="b")
        t = self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u1, "to_guid": t})
        st, _, _ = self.post("/associations?replace=true",
                             {"assoc_name": "owns", "from_guid": u2, "to_guid": t})
        self.assertEqual(st, 201)
        st, b, _ = self.get(f"/object/{t}/associations?relation=owner&expand=true")
        self.assertEqual(b["associations"][0]["neighbor"]["name"], "b")

    def test_delete_edge(self):
        self.assoc_schema("owns", "many_to_many")
        u, t = self.mk("user", name="a"), self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        st, _, _ = self.delete("/associations",
                               {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        self.assertEqual(st, 200)
        st, b, _ = self.get(f"/object/{u}/associations")
        self.assertEqual(b["total"], 0)

    def test_delete_object_cascades_edges(self):
        self.assoc_schema("owns", "many_to_many")
        u, t = self.mk("user", name="a"), self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        self.delete(f"/objects/task/{t}")
        st, b, _ = self.get(f"/object/{u}/associations")
        self.assertEqual(b["total"], 0)

    def test_direction_filter(self):
        self.assoc_schema("owns", "one_to_many")
        u = self.mk("user", name="a")
        t = self.mk("task", title="x")
        self.post("/associations", {"assoc_name": "owns", "from_guid": u, "to_guid": t})
        st, b, _ = self.get(f"/object/{u}/associations?direction=forward")
        self.assertEqual(b["total"], 1)
        st, b, _ = self.get(f"/object/{u}/associations?direction=inverse")
        self.assertEqual(b["total"], 0)


class TestCascades(Base):
    def test_delete_schema_removes_objects(self):
        self.post("/schemas/objects", {"name": "task", "fields": {"title": "string"}})
        self.post("/objects/task", {"title": "a"})
        st, b, _ = self.delete("/schemas/objects/task?cascade=true")
        self.assertEqual(st, 200)
        self.assertEqual(b["objects_removed"], 1)
        st, _, _ = self.get("/objects/task")
        self.assertEqual(st, 404)  # type gone

    def test_delete_schema_refuses_without_cascade(self):
        self.post("/schemas/objects", {"name": "task", "fields": {"title": "string"}})
        self.post("/objects/task", {"title": "a"})
        st, _, _ = self.delete("/schemas/objects/task?cascade=false")
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
