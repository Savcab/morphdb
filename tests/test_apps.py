"""Apps: registration, the required X-App-Key header, tenant isolation, and the
delete-app cascade. Decent coverage of the multi-tenant model end to end.
"""

import unittest

from morphdb import db
from harness import ensure_server, register_app, req


class AppBase(unittest.TestCase):
    """Fresh, empty db per test — no app auto-registered (these tests do it)."""

    def setUp(self):
        ensure_server()
        db.init_db(":memory:")


class TestRegistration(AppBase):
    def test_register_returns_201(self):
        st, b, _ = register_app("shop")
        self.assertEqual(st, 201, b)
        self.assertEqual(b, {"key": "shop", "created": True})

    def test_duplicate_key_rejected(self):
        self.assertEqual(register_app("shop")[0], 201)
        st, b, _ = register_app("shop")
        self.assertEqual(st, 409, b)

    def test_invalid_keys_rejected(self):
        for bad in ("", "has space", "_leading", "bad/slash", "x" * 129):
            st, _, _ = req("POST", "/app", {"key": bad}, app=None)
            self.assertEqual(st, 400, bad)

    def test_missing_key_in_body_rejected(self):
        st, _, _ = req("POST", "/app", {}, app=None)
        self.assertEqual(st, 400)

    def test_no_list_apps_endpoint(self):
        # Safety: there is intentionally no way to enumerate apps. /app exists
        # only for POST, so GET is method-not-allowed (never a listing).
        register_app("shop")
        st, _, _ = req("GET", "/app", app=None)
        self.assertEqual(st, 405)

    def test_delete_unknown_app_404(self):
        st, _, _ = req("DELETE", "/app/ghost", app=None)
        self.assertEqual(st, 404)


class TestAppKeyRequired(AppBase):
    def setUp(self):
        super().setUp()
        register_app("a")

    def test_missing_header_on_schema_400(self):
        st, b, _ = req("GET", "/schema", app=None)
        self.assertEqual(st, 400, b)

    def test_missing_header_on_object_400(self):
        st, _, _ = req("POST", "/objects/task", {"title": "x"}, app=None)
        self.assertEqual(st, 400)

    def test_unknown_app_404(self):
        st, b, _ = req("GET", "/schema", app="nope")
        self.assertEqual(st, 404, b)

    def test_malformed_app_header_400(self):
        st, _, _ = req("GET", "/schema", app="bad key")
        self.assertEqual(st, 400)


class TestIsolation(AppBase):
    def setUp(self):
        super().setUp()
        register_app("a")
        register_app("b")

    def _put_task(self, app, fields):
        st, b, _ = req("PUT", "/schema/task", {"fields": fields}, app=app)
        self.assertEqual(st, 200, b)

    def test_same_type_name_independent_across_apps(self):
        self._put_task("a", {"title": "string"})
        self._put_task("b", {"body": "string"})
        sa = req("GET", "/schema/task", app="a")[1]
        sb = req("GET", "/schema/task", app="b")[1]
        self.assertEqual(set(sa["fields"]), {"title"})
        self.assertEqual(set(sb["fields"]), {"body"})

    def test_objects_scoped_to_app(self):
        self._put_task("a", {"title": "string"})
        self._put_task("b", {"title": "string"})
        req("POST", "/objects/task", {"title": "only-in-a"}, app="a")
        self.assertEqual(req("GET", "/objects/task", app="a")[1]["total"], 1)
        self.assertEqual(req("GET", "/objects/task", app="b")[1]["total"], 0)

    def test_cross_app_guid_is_invisible(self):
        self._put_task("a", {"title": "string"})
        g = req("POST", "/objects/task", {"title": "x"}, app="a")[1]["_guid"]
        # readable in its own app, not in another
        self.assertEqual(req("GET", f"/object/{g}", app="a")[0], 200)
        self.assertEqual(req("GET", f"/object/{g}", app="b")[0], 404)
        self.assertEqual(req("GET", f"/objects/task/{g}", app="b")[0], 404)
        # and cannot be deleted from another app
        self.assertEqual(req("DELETE", f"/objects/task/{g}", app="b")[0], 404)
        self.assertEqual(req("GET", f"/object/{g}", app="a")[0], 200)

    def test_put_guid_owned_by_other_app_is_404_not_500(self):
        self._put_task("a", {"title": "string"})
        self._put_task("b", {"title": "string"})
        g = req("POST", "/objects/task", {"title": "x"}, app="a")[1]["_guid"]
        st, _, _ = req("PUT", f"/objects/task/{g}", {"title": "hijack"}, app="b")
        self.assertEqual(st, 404)            # not a 500 from a PK clash


class TestDeleteAppCascade(AppBase):
    def test_delete_app_cascades_everything(self):
        register_app("c")
        req("PUT", "/schema/user", {"fields": {"name": "string"}}, app="c")
        req("PUT", "/schema/task", {"fields": {"title": "string"}, "relations": {
            "assignee": {"to": "user", "cardinality": "many_to_one",
                         "inverse": "tasks"}}}, app="c")
        u = req("POST", "/objects/user", {"name": "Ann"}, app="c")[1]["_guid"]
        g = req("POST", "/objects/task", {"title": "x", "assignee": u},
                app="c")[1]["_guid"]
        # sanity: data is really there
        self.assertEqual(len(req("GET", "/schema", app="c")[1]["types"]), 2)

        st, b, _ = req("DELETE", "/app/c", app=None)
        self.assertEqual(st, 200, b)
        self.assertEqual(b, {"deleted": "c"})

        # FK ON DELETE CASCADE wiped every dependent row, in every table.
        c = db.conn()
        for table in ("object_schemas", "objects", "association_schemas", "associations"):
            n = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            self.assertEqual(n, 0, f"{table} not cascaded")

        # re-registering the same key gives a clean, empty app
        self.assertEqual(register_app("c")[0], 201)
        self.assertEqual(req("GET", "/schema", app="c")[1]["types"], [])
        self.assertEqual(req("GET", f"/object/{g}", app="c")[0], 404)


class TestCrossAppRelations(AppBase):
    def setUp(self):
        super().setUp()
        for app in ("a", "b"):
            register_app(app)
            req("PUT", "/schema/user", {"fields": {"name": "string"}}, app=app)
            req("PUT", "/schema/task", {"fields": {"title": "string"}, "relations": {
                "assignee": {"to": "user", "cardinality": "many_to_one",
                             "inverse": "tasks"}}}, app=app)

    def test_relation_target_must_be_same_app(self):
        ub = req("POST", "/objects/user", {"name": "Bob"}, app="b")[1]["_guid"]
        # task in app 'a' cannot point at a user that lives in app 'b'
        st, body, _ = req("POST", "/objects/task",
                          {"title": "x", "assignee": ub}, app="a")
        self.assertEqual(st, 400, body)
        # control: a same-app target works
        ua = req("POST", "/objects/user", {"name": "Ann"}, app="a")[1]["_guid"]
        st, _, _ = req("POST", "/objects/task", {"title": "y", "assignee": ua}, app="a")
        self.assertEqual(st, 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
