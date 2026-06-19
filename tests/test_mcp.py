"""MCP server: JSON-RPC dispatch, the tool catalog, every tool against a real
in-process backend, and the stdio read/write loop.

The MCP server is a thin HTTP client of the backend, so we point it at the shared
in-process harness server via ``MORPHDB_HOST`` (which also means ``_target`` never
tries to auto-start a real daemon during tests). Pure stdlib.
"""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness                                   # noqa: E402
from morphdb import db                            # noqa: E402
from morphdb.cli import mcp                        # noqa: E402

APP = "site"


def _rpc(method, mid=1, **params):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params:
        msg["params"] = params
    return mcp.handle(msg)


def _call(tool, **arguments):
    return mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})


def _payload(resp):
    """(is_error, text) from a tools/call response."""
    res = resp["result"]
    return res["isError"], res["content"][0]["text"]


def _ok_json(test, resp):
    """Assert not-an-error and return the parsed JSON body."""
    is_err, text = _payload(resp)
    test.assertFalse(is_err, text)
    return json.loads(text)


class _Base(unittest.TestCase):
    def setUp(self):
        harness.ensure_server()
        db.init_db(":memory:")
        self._env = {k: os.environ.get(k) for k in ("MORPHDB_HOST", "MORPHDB_APP")}
        os.environ["MORPHDB_HOST"] = harness.BASE      # point the MCP at the harness
        os.environ.pop("MORPHDB_APP", None)
        st, b, _ = harness.register_app(APP)
        self.assertEqual(st, 201, b)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- protocol -----------------------------------------------------------------


class TestProtocol(_Base):
    def test_initialize_reports_server_info(self):
        resp = _rpc("initialize", protocolVersion="2025-06-18",
                    capabilities={}, clientInfo={"name": "test"})
        r = resp["result"]
        self.assertEqual(r["serverInfo"]["name"], "morphdb")
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["id"], 1)

    def test_initialize_defaults_protocol_when_absent(self):
        r = _rpc("initialize")["result"]
        self.assertEqual(r["protocolVersion"], mcp._DEFAULT_PROTOCOL)

    def test_initialized_notification_gets_no_reply(self):
        self.assertIsNone(_rpc("notifications/initialized", mid=None))

    def test_ping(self):
        self.assertEqual(_rpc("ping")["result"], {})

    def test_unknown_method_is_error(self):
        resp = _rpc("does/not/exist")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_notification_is_silent(self):
        self.assertIsNone(_rpc("notifications/cancelled", mid=None))


class TestToolCatalog(_Base):
    def test_tools_list_shape(self):
        tools = _rpc("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        for expected in ("register_app", "delete_app",
                         "list_types", "describe_type",
                         "add_field", "drop_field",
                         "add_relation", "drop_relation",
                         "delete_type", "set_schema",
                         "query_objects"):
            self.assertIn(expected, names)
        for t in tools:
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIsInstance(t["inputSchema"]["required"], list)

    def test_handlers_match_catalog(self):
        self.assertEqual({t["name"] for t in mcp.TOOLS}, set(mcp.HANDLERS))


# --- tool execution -----------------------------------------------------------


class TestAppTools(_Base):
    def test_register_and_delete_app(self):
        is_err, text = _payload(_call("register_app", key="shop"))
        self.assertFalse(is_err, text)
        self.assertIn("shop", text)
        # cascade delete
        is_err, _ = _payload(_call("delete_app", key="shop"))
        self.assertFalse(is_err)

    def test_register_duplicate_surfaces_409(self):
        is_err, text = _payload(_call("register_app", key=APP))
        self.assertTrue(is_err)
        self.assertIn("409", text)

    def test_register_requires_key(self):
        is_err, text = _payload(_call("register_app"))
        self.assertTrue(is_err)
        self.assertIn("key", text)

    def test_delete_app_cascades_schema(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("delete_app", key=APP)
        # re-register: the type must be gone (cascade worked)
        harness.register_app(APP)
        body = _ok_json(self, _call("list_types", app=APP))
        self.assertEqual(body["types"], [])


class TestSchemaTools(_Base):
    def test_add_field_then_describe(self):
        _ok_json(self, _call("add_field", app=APP, type="task",
                             name="title", field_type="string"))
        doc = _ok_json(self, _call("describe_type", app=APP, type="task"))
        self.assertIn("title", doc["fields"])

    def test_add_field_default_and_required(self):
        _ok_json(self, _call("add_field", app=APP, type="task",
                             name="done", field_type="boolean", default=False))
        _ok_json(self, _call("add_field", app=APP, type="task",
                             name="title", field_type="string", required=True))
        doc = _ok_json(self, _call("describe_type", app=APP, type="task"))
        self.assertEqual(doc["fields"]["done"]["default"], False)
        self.assertTrue(doc["fields"]["title"].get("required"))

    def test_add_field_is_idempotent(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        is_err, _ = _payload(_call("add_field", app=APP, type="task",
                                   name="title", field_type="string"))
        self.assertFalse(is_err)

    def test_list_types(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        body = _ok_json(self, _call("list_types", app=APP))
        names = {t["name"] for t in body["types"]}
        self.assertIn("task", names)

    def test_drop_field(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="task", name="extra",
              field_type="number")
        _ok_json(self, _call("drop_field", app=APP, type="task",
                             name="extra"))
        doc = _ok_json(self, _call("describe_type", app=APP, type="task"))
        self.assertIn("title", doc["fields"])
        self.assertNotIn("extra", doc["fields"])

    def test_drop_missing_field_errors(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        is_err, text = _payload(_call("drop_field", app=APP, type="task",
                                      name="nope"))
        self.assertTrue(is_err)
        self.assertIn("nope", text)

    def test_add_relation_creates_inverse(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="user", name="name",
              field_type="string")
        _ok_json(self, _call("add_relation", app=APP, type="task",
                             name="assignee", to="user",
                             cardinality="many_to_one", inverse="tasks"))
        user = _ok_json(self, _call("describe_type", app=APP, type="user"))
        self.assertIn("tasks", user.get("inverse_relations", {}))

    def test_symmetric_relation(self):
        _call("add_field", app=APP, type="user", name="name",
              field_type="string")
        is_err, text = _payload(_call("add_relation", app=APP, type="user",
                                      name="friends", to="user",
                                      cardinality="many_to_many", symmetric=True))
        self.assertFalse(is_err, text)

    def test_relation_without_inverse_errors(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="user", name="name",
              field_type="string")
        is_err, text = _payload(_call("add_relation", app=APP, type="task",
                                      name="assignee", to="user",
                                      cardinality="many_to_one"))
        self.assertTrue(is_err)
        self.assertIn("inverse", text)

    def test_drop_relation(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="user", name="name",
              field_type="string")
        _call("add_relation", app=APP, type="task", name="assignee",
              to="user", cardinality="many_to_one", inverse="tasks")
        _ok_json(self, _call("drop_relation", app=APP, type="task",
                             name="assignee"))
        doc = _ok_json(self, _call("describe_type", app=APP, type="task"))
        self.assertNotIn("assignee", doc.get("relations", {}))

    def test_drop_relation_from_inverse_side_errors(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="user", name="name",
              field_type="string")
        _call("add_relation", app=APP, type="task", name="assignee",
              to="user", cardinality="many_to_one", inverse="tasks")
        # 'tasks' is the inverse, authored on 'task' — dropping from 'user' errors
        is_err, text = _payload(_call("drop_relation", app=APP, type="user",
                                      name="tasks"))
        self.assertTrue(is_err)
        self.assertIn("inverse", text)

    def test_delete_type(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _ok_json(self, _call("delete_type", app=APP, type="task"))
        body = _ok_json(self, _call("list_types", app=APP))
        self.assertEqual(body["types"], [])

    def test_set_schema_raw_doc(self):
        doc = {"merge": True, "fields": {"title": "string", "due": "datetime"}}
        _ok_json(self, _call("set_schema", app=APP, type="task", doc=doc))
        got = _ok_json(self, _call("describe_type", app=APP, type="task"))
        self.assertIn("due", got["fields"])

    def test_set_schema_rejects_non_object_doc(self):
        is_err, text = _payload(_call("set_schema", app=APP, type="task",
                                      doc="oops"))
        self.assertTrue(is_err)
        self.assertIn("doc", text)

    def test_describe_missing_type_surfaces_404(self):
        is_err, text = _payload(_call("describe_type", app=APP,
                                      type="ghost"))
        self.assertTrue(is_err)
        self.assertIn("404", text)


class TestQueryObjects(_Base):
    def test_query_objects_reads_data(self):
        _call("add_field", app=APP, type="task", name="title",
              field_type="string")
        _call("add_field", app=APP, type="task", name="done",
              field_type="boolean", default=False)
        # the frontend writes objects over HTTP; simulate that via the harness
        harness.req("POST", "/objects/task", {"title": "a", "done": False}, app=APP)
        harness.req("POST", "/objects/task", {"title": "b", "done": True}, app=APP)
        body = _ok_json(self, _call("query_objects", app=APP, type="task"))
        self.assertEqual(body["total"], 2)
        filtered = _ok_json(self, _call("query_objects", app=APP,
                                        type="task", query="done=true"))
        self.assertEqual(filtered["total"], 1)


class TestAppKeyResolution(_Base):
    def test_app_arg_used(self):
        _ok_json(self, _call("list_types", app=APP))

    def test_falls_back_to_env_app(self):
        os.environ["MORPHDB_APP"] = APP
        _ok_json(self, _call("list_types"))     # no app arg

    def test_missing_app_errors(self):
        is_err, text = _payload(_call("list_types"))   # no arg, no env
        self.assertTrue(is_err)
        self.assertIn("app key", text)

    def test_unknown_app_surfaces_404(self):
        is_err, text = _payload(_call("list_types", app="never-registered"))
        self.assertTrue(is_err)
        self.assertIn("404", text)


class TestToolErrors(_Base):
    def test_unknown_tool(self):
        is_err, text = _payload(_call("nope"))
        self.assertTrue(is_err)
        self.assertIn("unknown tool", text)

    def test_bad_arguments_type(self):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "list_types",
                                      "arguments": "notdict"}})
        is_err, text = _payload(resp)
        self.assertTrue(is_err)


# --- stdio loop ---------------------------------------------------------------


class TestServeLoop(_Base):
    def _run(self, lines):
        stdin = io.StringIO("".join(l + "\n" for l in lines))
        stdout = io.StringIO()
        mcp.serve(stdin, stdout)
        out = stdout.getvalue()
        return [json.loads(l) for l in out.splitlines() if l.strip()], out

    def test_roundtrip_initialize_then_call(self):
        responses, _ = self._run([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "add_field",
                                   "arguments": {"app": APP, "type": "task",
                                                 "name": "title",
                                                 "field_type": "string"}}}),
        ])
        # initialize + tools/call → 2 responses; the notification produced none
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["id"], 2)
        self.assertFalse(responses[1]["result"]["isError"])

    def test_blank_lines_skipped(self):
        responses, _ = self._run(["", "  ", json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"})])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 9)

    def test_parse_error_line(self):
        responses, _ = self._run(["{not json}"])
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_responses_are_single_lines(self):
        # a multi-line tool result must still serialize to ONE physical line
        _, out = self._run([json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "add_field",
                        "arguments": {"app": APP, "type": "task",
                                      "name": "title", "field_type": "string"}}})])
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(len(out.rstrip("\n").splitlines()), 1)


# --- backend target resolution (no MORPHDB_HOST) ------------------------------


class TestTarget(unittest.TestCase):
    def setUp(self):
        self._h = os.environ.pop("MORPHDB_HOST", None)

    def tearDown(self):
        if self._h is not None:
            os.environ["MORPHDB_HOST"] = self._h
        else:
            os.environ.pop("MORPHDB_HOST", None)

    def test_hosted_url_used_as_is(self):
        os.environ["MORPHDB_HOST"] = "https://db.example.com"
        self.assertEqual(mcp._target(), "https://db.example.com")

    def test_bare_host_gets_http(self):
        os.environ["MORPHDB_HOST"] = "1.2.3.4:9999"
        self.assertEqual(mcp._target(), "http://1.2.3.4:9999")

    def test_local_running_does_not_autostart(self):
        from morphdb.cli import service
        orig = (service.status, service.start)
        started = []
        service.status = lambda: {"running": True, "host": "127.0.0.1", "port": 7777}
        service.start = lambda *a, **k: started.append(1) or ({}, True)
        try:
            self.assertEqual(mcp._target(), "http://127.0.0.1:7777")
            self.assertEqual(started, [])
        finally:
            service.status, service.start = orig

    def test_local_down_autostarts(self):
        from morphdb.cli import service
        orig = (service.status, service.start)
        started = []
        service.status = lambda: {"running": False}

        def fake_start(*a, **k):
            started.append(1)
            return {"running": True, "host": "127.0.0.1", "port": 8787}, True

        service.start = fake_start
        try:
            self.assertEqual(mcp._target(), "http://127.0.0.1:8787")
            self.assertEqual(started, [1])
        finally:
            service.status, service.start = orig


class TestRegistrationSummary(unittest.TestCase):
    def test_does_not_crash_and_returns_str(self):
        self.assertIsInstance(mcp.registration_summary(), str)

    def test_detects_in_config(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, ".mcp.json")
        with open(path, "w") as f:
            json.dump({"mcpServers": {"morphdb": {"command": "morphdb"}}}, f)
        self.assertTrue(mcp._config_has_morphdb(path))
        with open(path, "w") as f:
            json.dump({"mcpServers": {"other": {}}}, f)
        self.assertFalse(mcp._config_has_morphdb(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
