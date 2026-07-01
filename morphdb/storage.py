"""Logical store interface for MorphDB engines.

The SQL engines keep the existing relational tables underneath. DynamoDB uses
engine-native key/value items. Domain code should ask for MorphDB concepts
(apps, schemas, objects, field-index rows, relation edges) instead of issuing SQL
directly.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from urllib.parse import quote, unquote


def _rowdict(row):
    if row is None:
        return None
    return dict(row)


def _rows(cur):
    return [_rowdict(r) for r in cur.fetchall()]


def _enc(v):
    return quote(str(v), safe="")


def _dec(v):
    return unquote(v)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _json_default(v):
    if isinstance(v, Decimal):
        if v % 1 == 0:
            return int(v)
        return float(v)
    raise TypeError(f"Object of type {type(v).__name__} is not JSON serializable")


def _to_ddb(v):
    """Convert Python values into boto3 DynamoDB-resource-safe values."""
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _to_ddb(val) for k, val in v.items() if val is not None}
    if isinstance(v, list):
        return [_to_ddb(val) for val in v]
    return v


def _from_ddb(v):
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _from_ddb(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_from_ddb(val) for val in v]
    return v


def encode_index_value(ftype, value):
    """Stable value encoding for DynamoDB sort keys.

    The implementation preserves simple lexical order for strings/datetimes and
    booleans. Numeric range/sort callers must still verify in Python; preserving
    total numeric ordering for arbitrary ints/floats in a string key is subtle,
    and correctness wins over cleverness here.
    """
    if ftype == "boolean":
        return "B#1" if value else "B#0"
    if ftype == "number":
        return f"N#{_enc(json.dumps(value, default=_json_default, separators=(',', ':')))}"
    if ftype == "datetime":
        return f"D#{_enc(value)}"
    return f"S#{_enc(value)}"


def decode_edge_id(eid):
    parts = str(eid).split("\x1f", 3)
    if len(parts) != 4:
        return None
    return tuple(parts)


def edge_id(app, assoc_name, from_guid, to_guid):
    return "\x1f".join((app, assoc_name, from_guid, to_guid))


class SqlStore:
    name = "sql"

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    # -- apps -----------------------------------------------------------------

    def list_apps(self):
        return _rows(self.conn.execute("SELECT key, created_at FROM apps ORDER BY key"))

    def app_exists(self, key):
        return self.conn.execute("SELECT 1 FROM apps WHERE key = ?", (key,)).fetchone() is not None

    def create_app(self, key, created_at):
        self.conn.execute("INSERT INTO apps (key, created_at) VALUES (?, ?)", (key, created_at))

    def delete_app(self, key):
        self.conn.execute("DELETE FROM apps WHERE key = ?", (key,))

    # -- schemas --------------------------------------------------------------

    def get_object_schema(self, app, name):
        return _rowdict(self.conn.execute(
            "SELECT * FROM object_schemas WHERE app = ? AND name = ?", (app, name)
        ).fetchone())

    def list_object_schema_names(self, app):
        return [r["name"] for r in self.conn.execute(
            "SELECT name FROM object_schemas WHERE app = ? ORDER BY name", (app,)
        ).fetchall()]

    def list_object_schemas(self, app):
        return _rows(self.conn.execute(
            "SELECT * FROM object_schemas WHERE app = ? ORDER BY name", (app,)
        ))

    def put_object_schema(self, app, name, fields_json, created_at, updated_at, exists):
        if exists:
            self.conn.execute(
                "UPDATE object_schemas SET fields = ?, updated_at = ? "
                "WHERE app = ? AND name = ?",
                (fields_json, updated_at, app, name),
            )
        else:
            self.conn.execute(
                "INSERT INTO object_schemas (app, name, fields, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (app, name, fields_json, created_at, updated_at),
            )

    def delete_object_schema(self, app, name):
        self.conn.execute("DELETE FROM object_schemas WHERE app = ? AND name = ?", (app, name))

    # -- association schemas --------------------------------------------------

    def list_association_schemas(self, app):
        return _rows(self.conn.execute("SELECT * FROM association_schemas WHERE app=?", (app,)))

    def get_association_schema(self, app, name):
        return _rowdict(self.conn.execute(
            "SELECT * FROM association_schemas WHERE app=? AND name=?", (app, name)
        ).fetchone())

    def list_association_schemas_from_type(self, app, from_type):
        return _rows(self.conn.execute(
            "SELECT * FROM association_schemas WHERE app=? AND from_type=?",
            (app, from_type),
        ))

    def list_association_schemas_touching_type(self, app, type_name):
        return _rows(self.conn.execute(
            "SELECT * FROM association_schemas WHERE app=? AND (from_type=? OR to_type=?)",
            (app, type_name, type_name),
        ))

    def put_association_schema(self, app, d, ts, exists):
        if exists:
            self.conn.execute(
                "UPDATE association_schemas SET from_type=?, to_type=?, forward_name=?, "
                "inverse_name=?, cardinality=?, \"symmetric\"=?, forward_description=?, "
                "inverse_description=?, updated_at=? WHERE app=? AND name=?",
                (d["from_type"], d["to_type"], d["forward_name"], d["inverse_name"],
                 d["cardinality"], int(d["symmetric"]), d["forward_description"],
                 d["inverse_description"], ts, app, d["name"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO association_schemas (app, name, from_type, to_type, "
                "forward_name, inverse_name, cardinality, \"symmetric\", "
                "forward_description, inverse_description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (app, d["name"], d["from_type"], d["to_type"], d["forward_name"],
                 d["inverse_name"], d["cardinality"], int(d["symmetric"]),
                 d["forward_description"], d["inverse_description"], ts, ts),
            )

    def delete_association_schema(self, app, name):
        self.conn.execute("DELETE FROM association_schemas WHERE app=? AND name=?", (app, name))

    def delete_association_schemas_touching_type(self, app, type_name):
        self.conn.execute(
            "DELETE FROM association_schemas WHERE app=? AND (from_type=? OR to_type=?)",
            (app, type_name, type_name),
        )

    # -- objects --------------------------------------------------------------

    def get_object_by_guid_any_app(self, guid):
        return _rowdict(self.conn.execute("SELECT * FROM objects WHERE guid = ?", (guid,)).fetchone())

    def get_object(self, app, guid):
        return _rowdict(self.conn.execute(
            "SELECT * FROM objects WHERE app = ? AND guid = ?", (app, guid)
        ).fetchone())

    def insert_object(self, guid, app, object_type, data_json, created_at, updated_at):
        self.conn.execute(
            "INSERT INTO objects (guid, app, object_type, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guid, app, object_type, data_json, created_at, updated_at),
        )

    def update_object(self, guid, data_json, updated_at):
        self.conn.execute(
            "UPDATE objects SET data = ?, updated_at = ? WHERE guid = ?",
            (data_json, updated_at, guid),
        )

    def delete_object(self, app, guid):
        self.conn.execute("DELETE FROM objects WHERE app = ? AND guid = ?", (app, guid))

    def list_objects(self, app=None, object_type=None):
        clauses, params = [], []
        if app is not None:
            clauses.append("app = ?")
            params.append(app)
        if object_type is not None:
            clauses.append("object_type = ?")
            params.append(object_type)
        sql = "SELECT * FROM objects"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return _rows(self.conn.execute(sql, params))

    def delete_objects_by_guids(self, app, guids):
        if not guids:
            return
        for part in _chunks(list(guids), 400):
            qmarks = ",".join("?" * len(part))
            self.conn.execute(f"DELETE FROM objects WHERE app = ? AND guid IN ({qmarks})",
                              [app, *part])

    # -- field index ----------------------------------------------------------

    def delete_field_index_for_object(self, guid):
        self.conn.execute("DELETE FROM field_index WHERE object_id = ?", (guid,))

    def insert_field_index_rows(self, rows):
        if rows:
            self.conn.executemany(
                "INSERT INTO field_index "
                "(app, object_id, object_type, field_name, str_val, num_val, bool_val) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def delete_field_index_for_field(self, app, object_type, field_name):
        self.conn.execute(
            "DELETE FROM field_index WHERE app = ? AND object_type = ? AND field_name = ?",
            (app, object_type, field_name),
        )

    def delete_field_index_for_app(self, app=None):
        if app is None:
            self.conn.execute("DELETE FROM field_index")
        else:
            self.conn.execute("DELETE FROM field_index WHERE app = ?", (app,))

    # -- associations ---------------------------------------------------------

    def list_edges(self, app, assoc_name):
        rows = _rows(self.conn.execute(
            "SELECT id, from_guid, to_guid, created_at FROM associations "
            "WHERE app=? AND assoc_name=? ORDER BY created_at, id",
            (app, assoc_name),
        ))
        for r in rows:
            r["id"] = r["id"]
        return rows

    def list_edges_touching_guids(self, app, assoc_name, guids):
        if not guids:
            return []
        out = []
        for part in _chunks(list(guids), 400):
            qmarks = ",".join("?" * len(part))
            out.extend(_rows(self.conn.execute(
                f"SELECT id, from_guid, to_guid, created_at FROM associations "
                f"WHERE app=? AND assoc_name=? "
                f"AND (from_guid IN ({qmarks}) OR to_guid IN ({qmarks})) "
                f"ORDER BY created_at, id",
                [app, assoc_name, *part, *part],
            )))
        return out

    def list_edges_for_object_side(self, app, assoc_name, side, obj_guid):
        if side == "from":
            return _rows(self.conn.execute(
                "SELECT id, from_guid, to_guid, created_at FROM associations "
                "WHERE app=? AND assoc_name=? AND from_guid=?",
                (app, assoc_name, obj_guid),
            ))
        if side == "to":
            return _rows(self.conn.execute(
                "SELECT id, from_guid, to_guid, created_at FROM associations "
                "WHERE app=? AND assoc_name=? AND to_guid=?",
                (app, assoc_name, obj_guid),
            ))
        return _rows(self.conn.execute(
            "SELECT id, from_guid, to_guid, created_at FROM associations "
            "WHERE app=? AND assoc_name=? AND (from_guid=? OR to_guid=?)",
            (app, assoc_name, obj_guid, obj_guid),
        ))

    def insert_edge_ignore(self, app, assoc_name, from_guid, to_guid, created_at):
        self.conn.execute(
            "INSERT OR IGNORE INTO associations (app, assoc_name, from_guid, to_guid, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (app, assoc_name, from_guid, to_guid, created_at))

    def delete_edge_by_id(self, eid):
        self.conn.execute("DELETE FROM associations WHERE id=?", (eid,))

    def delete_edges_for_assoc(self, app, assoc_name):
        self.conn.execute("DELETE FROM associations WHERE app=? AND assoc_name=?",
                          (app, assoc_name))

    def delete_edges_touching_object(self, app, guid):
        self.conn.execute(
            "DELETE FROM associations WHERE app = ? AND (from_guid = ? OR to_guid = ?)",
            (app, guid, guid),
        )

    def delete_edges_for_target_slot(self, app, assoc_name, side, neighbor):
        if side == "from":
            self.conn.execute("DELETE FROM associations WHERE app=? AND assoc_name=? AND to_guid=?",
                              (app, assoc_name, neighbor))
        elif side == "to":
            self.conn.execute("DELETE FROM associations WHERE app=? AND assoc_name=? AND from_guid=?",
                              (app, assoc_name, neighbor))
        else:
            self.conn.execute(
                "DELETE FROM associations WHERE app=? AND assoc_name=? AND (from_guid=? OR to_guid=?)",
                (app, assoc_name, neighbor, neighbor),
            )


class DynamoStore:
    name = "dynamodb"

    def __init__(self, raw):
        self.raw = raw
        self.table = raw.table
        self._undo = None

    def begin(self):
        self._undo = []

    def commit(self):
        self._undo = None

    def rollback(self):
        undo = self._undo or []
        self._undo = None
        for action, payload in reversed(undo):
            if action == "put":
                self.table.put_item(Item=_to_ddb(payload))
            else:
                self.table.delete_item(Key=payload)

    # -- key helpers ----------------------------------------------------------

    def _shard(self, guid):
        return str(guid)[-2:] if len(str(guid)) >= 2 else "00"

    def _object_key(self, app, guid):
        return {"pk": f"APP#{_enc(app)}#OBJ#{self._shard(guid)}",
                "sk": f"GUID#{_enc(guid)}"}

    def _query_all(self, **kwargs):
        items = []
        while True:
            res = self.table.query(**kwargs)
            items.extend(_from_ddb(i) for i in res.get("Items", []))
            lek = res.get("LastEvaluatedKey")
            if not lek:
                return items
            kwargs["ExclusiveStartKey"] = lek

    def _scan_all(self, **kwargs):
        items = []
        while True:
            res = self.table.scan(**kwargs)
            items.extend(_from_ddb(i) for i in res.get("Items", []))
            lek = res.get("LastEvaluatedKey")
            if not lek:
                return items
            kwargs["ExclusiveStartKey"] = lek

    def _put(self, item, **kwargs):
        before = None
        if self._undo is not None:
            key = {"pk": item["pk"], "sk": item["sk"]}
            before = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        self.table.put_item(Item=_to_ddb(item), **kwargs)
        if self._undo is not None:
            if before:
                self._undo.append(("put", _from_ddb(before)))
            else:
                self._undo.append(("delete", {"pk": item["pk"], "sk": item["sk"]}))

    def _delete(self, key):
        before = None
        if self._undo is not None:
            before = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        self.table.delete_item(Key=key)
        if self._undo is not None and before:
            self._undo.append(("put", _from_ddb(before)))

    def _batch_write(self, puts=(), deletes=()):
        if self._undo is not None:
            for item in puts:
                self._put(item)
            for key in deletes:
                self._delete(key)
            return
        with self.table.batch_writer() as batch:
            for item in puts:
                batch.put_item(Item=_to_ddb(item))
            for key in deletes:
                batch.delete_item(Key=key)

    def _batch_get(self, keys):
        out = []
        for part in _chunks(list(keys), 100):
            request = {self.raw.table_name: {"Keys": part}}
            delay = 0.05
            while request.get(self.raw.table_name, {}).get("Keys"):
                res = self.raw.resource.batch_get_item(RequestItems=request)
                out.extend(_from_ddb(i) for i in res.get("Responses", {}).get(self.raw.table_name, []))
                request = res.get("UnprocessedKeys", {})
                if request.get(self.raw.table_name, {}).get("Keys"):
                    time.sleep(delay)
                    delay = min(delay * 2, 1.0)
        return out

    # -- apps -----------------------------------------------------------------

    def list_apps(self):
        items = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "APPS"})
        rows = [{"key": i["key"], "created_at": i["created_at"]} for i in items]
        return sorted(rows, key=lambda r: r["key"])

    def app_exists(self, key):
        res = self.table.get_item(Key={"pk": f"APP#{_enc(key)}", "sk": "META"},
                                  ConsistentRead=True)
        return "Item" in res

    def create_app(self, key, created_at):
        self._put({
            "pk": f"APP#{_enc(key)}", "sk": "META", "kind": "app",
            "key": key, "created_at": created_at,
            "gsi1pk": f"APP#{_enc(key)}", "gsi1sk": "META",
        }, ConditionExpression="attribute_not_exists(pk)")
        self._put({
            "pk": "APPS", "sk": f"APP#{_enc(key)}", "kind": "app_ref",
            "key": key, "created_at": created_at,
        })

    def delete_app(self, key):
        app_pk = f"APP#{_enc(key)}"
        to_delete = [{"pk": app_pk, "sk": "META"}, {"pk": "APPS", "sk": f"APP#{_enc(key)}"}]
        for item in self._scan_all(
                FilterExpression="gsi1pk = :app",
                ExpressionAttributeValues={":app": app_pk}):
            to_delete.append({"pk": item["pk"], "sk": item["sk"]})
            if item.get("kind") == "object":
                to_delete.append({"pk": f"GUID#{_enc(item['guid'])}", "sk": "OWNER"})
        self._batch_write(deletes=to_delete)

    # -- schemas --------------------------------------------------------------

    def get_object_schema(self, app, name):
        res = self.table.get_item(
            Key={"pk": f"APP#{_enc(app)}#SCHEMA", "sk": f"TYPE#{_enc(name)}"},
            ConsistentRead=True)
        return self._schema_row(res.get("Item"))

    def _schema_row(self, item):
        if not item:
            return None
        item = _from_ddb(item)
        return {"app": item["app"], "name": item["name"], "fields": item["fields"],
                "created_at": item["created_at"], "updated_at": item["updated_at"]}

    def list_object_schema_names(self, app):
        rows = self.list_object_schemas(app)
        return sorted(r["name"] for r in rows)

    def list_object_schemas(self, app):
        items = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"APP#{_enc(app)}#SCHEMA"})
        return sorted((self._schema_row(i) for i in items), key=lambda r: r["name"])

    def put_object_schema(self, app, name, fields_json, created_at, updated_at, exists):
        self._put({
            "pk": f"APP#{_enc(app)}#SCHEMA", "sk": f"TYPE#{_enc(name)}",
            "kind": "object_schema", "app": app, "name": name,
            "fields": fields_json, "created_at": created_at, "updated_at": updated_at,
            "gsi1pk": f"APP#{_enc(app)}", "gsi1sk": f"SCHEMA#TYPE#{_enc(name)}",
        })

    def delete_object_schema(self, app, name):
        self._delete({"pk": f"APP#{_enc(app)}#SCHEMA", "sk": f"TYPE#{_enc(name)}"})

    # -- association schemas --------------------------------------------------

    def _assoc_schema_row(self, item):
        if not item:
            return None
        item = _from_ddb(item)
        return {
            "app": item["app"], "name": item["name"],
            "from_type": item["from_type"], "to_type": item["to_type"],
            "forward_name": item["forward_name"], "inverse_name": item["inverse_name"],
            "cardinality": item["cardinality"], "symmetric": int(bool(item["symmetric"])),
            "forward_description": item.get("forward_description"),
            "inverse_description": item.get("inverse_description"),
            "created_at": item["created_at"], "updated_at": item["updated_at"],
        }

    def list_association_schemas(self, app):
        items = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"APP#{_enc(app)}#ASSOC_SCHEMA"})
        return [self._assoc_schema_row(i) for i in items]

    def get_association_schema(self, app, name):
        res = self.table.get_item(
            Key={"pk": f"APP#{_enc(app)}#ASSOC_SCHEMA", "sk": f"ASSOC#{_enc(name)}"},
            ConsistentRead=True)
        return self._assoc_schema_row(res.get("Item"))

    def list_association_schemas_from_type(self, app, from_type):
        return [r for r in self.list_association_schemas(app) if r["from_type"] == from_type]

    def list_association_schemas_touching_type(self, app, type_name):
        return [r for r in self.list_association_schemas(app)
                if r["from_type"] == type_name or r["to_type"] == type_name]

    def put_association_schema(self, app, d, ts, exists):
        item = {
            "pk": f"APP#{_enc(app)}#ASSOC_SCHEMA", "sk": f"ASSOC#{_enc(d['name'])}",
            "kind": "association_schema", "app": app, "name": d["name"],
            "from_type": d["from_type"], "to_type": d["to_type"],
            "forward_name": d["forward_name"], "inverse_name": d["inverse_name"],
            "cardinality": d["cardinality"], "symmetric": bool(d["symmetric"]),
            "created_at": ts if not exists else (self.get_association_schema(app, d["name"]) or {}).get("created_at", ts),
            "updated_at": ts, "gsi1pk": f"APP#{_enc(app)}",
            "gsi1sk": f"ASSOC_SCHEMA#ASSOC#{_enc(d['name'])}",
        }
        if d.get("forward_description") is not None:
            item["forward_description"] = d["forward_description"]
        if d.get("inverse_description") is not None:
            item["inverse_description"] = d["inverse_description"]
        self._put(item)

    def delete_association_schema(self, app, name):
        self._delete({"pk": f"APP#{_enc(app)}#ASSOC_SCHEMA", "sk": f"ASSOC#{_enc(name)}"})

    def delete_association_schemas_touching_type(self, app, type_name):
        for r in self.list_association_schemas_touching_type(app, type_name):
            self.delete_association_schema(app, r["name"])

    # -- objects --------------------------------------------------------------

    def _object_row(self, item):
        if not item:
            return None
        item = _from_ddb(item)
        return {"guid": item["guid"], "app": item["app"], "object_type": item["object_type"],
                "data": item["data"], "created_at": item["created_at"],
                "updated_at": item["updated_at"]}

    def get_object_by_guid_any_app(self, guid):
        owner = self.table.get_item(Key={"pk": f"GUID#{_enc(guid)}", "sk": "OWNER"},
                                    ConsistentRead=True).get("Item")
        if not owner:
            return None
        owner = _from_ddb(owner)
        return self.get_object(owner["app"], guid)

    def get_object(self, app, guid):
        res = self.table.get_item(Key=self._object_key(app, guid), ConsistentRead=True)
        return self._object_row(res.get("Item"))

    def insert_object(self, guid, app, object_type, data_json, created_at, updated_at):
        source_key = self._object_key(app, guid)
        source = {
            **source_key, "kind": "object", "app": app, "guid": guid,
            "object_type": object_type, "data": data_json,
            "created_at": created_at, "updated_at": updated_at,
            "gsi1pk": f"APP#{_enc(app)}", "gsi1sk": f"OBJ#TYPE#{_enc(object_type)}#GUID#{_enc(guid)}",
        }
        list_ref = {
            "pk": f"APP#{_enc(app)}#TYPE#{_enc(object_type)}",
            "sk": f"OBJ#C#{created_at}#G#{_enc(guid)}",
            "kind": "object_ref", "app": app, "guid": guid, "object_type": object_type,
            "source_pk": source_key["pk"], "source_sk": source_key["sk"],
            "created_at": created_at, "updated_at": updated_at,
            "gsi1pk": f"APP#{_enc(app)}", "gsi1sk": f"OBJ_REF#TYPE#{_enc(object_type)}#GUID#{_enc(guid)}",
            "gsi2pk": f"APP#{_enc(app)}#TYPE#{_enc(object_type)}",
            "gsi2sk": f"OBJ#U#{updated_at}#G#{_enc(guid)}",
        }
        owner = {"pk": f"GUID#{_enc(guid)}", "sk": "OWNER", "kind": "guid_owner",
                 "guid": guid, "app": app, "object_type": object_type, "created_at": created_at}
        self._put(owner, ConditionExpression="attribute_not_exists(pk)")
        self._batch_write(puts=[source, list_ref])

    def update_object(self, guid, data_json, updated_at):
        existing = self.get_object_by_guid_any_app(guid)
        if not existing:
            return
        key = self._object_key(existing["app"], guid)
        item = _from_ddb(self.table.get_item(Key=key, ConsistentRead=True).get("Item"))
        item["data"] = data_json
        item["updated_at"] = updated_at
        self._put(item)
        refs = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"APP#{_enc(existing['app'])}#TYPE#{_enc(existing['object_type'])}"})
        for ref in refs:
            if ref.get("guid") == guid:
                ref["updated_at"] = updated_at
                ref["gsi2sk"] = f"OBJ#U#{updated_at}#G#{_enc(guid)}"
                self._put(ref)
                break

    def delete_object(self, app, guid):
        row = self.get_object(app, guid)
        if not row:
            return
        deletes = [self._object_key(app, guid),
                   {"pk": f"GUID#{_enc(guid)}", "sk": "OWNER"}]
        refs = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"APP#{_enc(app)}#TYPE#{_enc(row['object_type'])}"})
        for ref in refs:
            if ref.get("guid") == guid:
                deletes.append({"pk": ref["pk"], "sk": ref["sk"]})
        self._batch_write(deletes=deletes)

    def list_objects(self, app=None, object_type=None):
        if app is not None and object_type is not None:
            refs = self._query_all(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": f"APP#{_enc(app)}#TYPE#{_enc(object_type)}"})
            keys = [{"pk": r["source_pk"], "sk": r["source_sk"]} for r in refs]
            rows = [self._object_row(i) for i in self._batch_get(keys)]
            return [r for r in rows if r is not None]
        filt, vals = [], {}
        if app is not None:
            filt.append("app = :app")
            vals[":app"] = app
        if object_type is not None:
            filt.append("object_type = :typ")
            vals[":typ"] = object_type
        kwargs = {"FilterExpression": "kind = :kind", "ExpressionAttributeValues": {":kind": "object"}}
        if filt:
            kwargs["FilterExpression"] += " AND " + " AND ".join(filt)
            kwargs["ExpressionAttributeValues"].update(vals)
        return [self._object_row(i) for i in self._scan_all(**kwargs)]

    def delete_objects_by_guids(self, app, guids):
        for guid in list(guids):
            self.delete_object(app, guid)

    # -- field index ----------------------------------------------------------

    def _field_index_key(self, app, object_type, field_name, guid, ftype, value):
        return {
            "pk": f"FIDX#APP#{_enc(app)}#TYPE#{_enc(object_type)}#FIELD#{_enc(field_name)}#VT#{_enc(ftype)}",
            "sk": f"VAL#{encode_index_value(ftype, value)}#G#{_enc(guid)}",
        }

    def _field_index_items_for_object(self, guid):
        return self._scan_all(
            FilterExpression="kind = :kind AND guid = :guid",
            ExpressionAttributeValues={":kind": "field_index", ":guid": guid})

    def delete_field_index_for_object(self, guid):
        self._batch_write(deletes=[{"pk": i["pk"], "sk": i["sk"]}
                                   for i in self._field_index_items_for_object(guid)])

    def insert_field_index_rows(self, rows):
        puts = []
        for app, guid, object_type, field_name, str_val, num_val, bool_val in rows:
            if str_val is not None:
                ftype, value = "string", str_val
            elif num_val is not None:
                ftype, value = "number", num_val
            elif bool_val is not None:
                ftype, value = "boolean", bool(bool_val)
            else:
                continue
            key = self._field_index_key(app, object_type, field_name, guid, ftype, value)
            puts.append({
                **key, "kind": "field_index", "app": app, "object_type": object_type,
                "field_name": field_name, "guid": guid, "value_type": ftype,
                "value_json": json.dumps(value, default=_json_default),
                "gsi1pk": f"APP#{_enc(app)}",
                "gsi1sk": f"FIDX#TYPE#{_enc(object_type)}#FIELD#{_enc(field_name)}#GUID#{_enc(guid)}",
            })
        self._batch_write(puts=puts)

    def delete_field_index_for_field(self, app, object_type, field_name):
        items = self._scan_all(
            FilterExpression="kind = :kind AND app = :app AND object_type = :typ AND field_name = :field",
            ExpressionAttributeValues={":kind": "field_index", ":app": app,
                                       ":typ": object_type, ":field": field_name})
        self._batch_write(deletes=[{"pk": i["pk"], "sk": i["sk"]} for i in items])

    def delete_field_index_for_app(self, app=None):
        expr = "kind = :kind"
        vals = {":kind": "field_index"}
        if app is not None:
            expr += " AND app = :app"
            vals[":app"] = app
        items = self._scan_all(FilterExpression=expr, ExpressionAttributeValues=vals)
        self._batch_write(deletes=[{"pk": i["pk"], "sk": i["sk"]} for i in items])

    # -- associations ---------------------------------------------------------

    def _edge_key(self, app, assoc_name, from_guid, to_guid):
        return {"pk": f"EDGE#APP#{_enc(app)}#ASSOC#{_enc(assoc_name)}",
                "sk": f"FROM#{_enc(from_guid)}#TO#{_enc(to_guid)}"}

    def _edge_row(self, item):
        item = _from_ddb(item)
        return {"id": edge_id(item["app"], item["assoc_name"], item["from_guid"], item["to_guid"]),
                "from_guid": item["from_guid"], "to_guid": item["to_guid"],
                "created_at": item["created_at"]}

    def list_edges(self, app, assoc_name):
        items = self._query_all(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"EDGE#APP#{_enc(app)}#ASSOC#{_enc(assoc_name)}"})
        return sorted(
            [self._edge_row(i) for i in items],
            key=lambda r: (r.get("created_at"), str(r.get("id"))),
        )

    def list_edges_touching_guids(self, app, assoc_name, guids):
        gset = set(guids)
        return [r for r in self.list_edges(app, assoc_name)
                if r["from_guid"] in gset or r["to_guid"] in gset]

    def list_edges_for_object_side(self, app, assoc_name, side, obj_guid):
        if side == "from":
            return [r for r in self.list_edges(app, assoc_name) if r["from_guid"] == obj_guid]
        if side == "to":
            return [r for r in self.list_edges(app, assoc_name) if r["to_guid"] == obj_guid]
        return [r for r in self.list_edges(app, assoc_name)
                if r["from_guid"] == obj_guid or r["to_guid"] == obj_guid]

    def insert_edge_ignore(self, app, assoc_name, from_guid, to_guid, created_at):
        key = self._edge_key(app, assoc_name, from_guid, to_guid)
        item = {**key, "kind": "edge", "app": app, "assoc_name": assoc_name,
                "from_guid": from_guid, "to_guid": to_guid, "created_at": created_at,
                "gsi1pk": f"APP#{_enc(app)}",
                "gsi1sk": f"EDGE#ASSOC#{_enc(assoc_name)}#FROM#{_enc(from_guid)}#TO#{_enc(to_guid)}"}
        try:
            self._put(item, ConditionExpression="attribute_not_exists(pk)")
        except self.raw.client.exceptions.ConditionalCheckFailedException:
            pass

    def delete_edge_by_id(self, eid):
        parsed = decode_edge_id(eid)
        if not parsed:
            return
        app, assoc_name, from_guid, to_guid = parsed
        self._delete(self._edge_key(app, assoc_name, from_guid, to_guid))

    def delete_edges_for_assoc(self, app, assoc_name):
        self._batch_write(deletes=[self._edge_key(app, assoc_name, r["from_guid"], r["to_guid"])
                                   for r in self.list_edges(app, assoc_name)])

    def delete_edges_touching_object(self, app, guid):
        deletes = []
        for s in self.list_association_schemas(app):
            for r in self.list_edges(s["app"], s["name"]):
                if r["from_guid"] == guid or r["to_guid"] == guid:
                    deletes.append(self._edge_key(app, s["name"], r["from_guid"], r["to_guid"]))
        self._batch_write(deletes=deletes)

    def delete_edges_for_target_slot(self, app, assoc_name, side, neighbor):
        rows = self.list_edges(app, assoc_name)
        deletes = []
        for r in rows:
            if (side == "from" and r["to_guid"] == neighbor) or \
               (side == "to" and r["from_guid"] == neighbor) or \
               (side == "sym" and (r["from_guid"] == neighbor or r["to_guid"] == neighbor)):
                deletes.append(self._edge_key(app, assoc_name, r["from_guid"], r["to_guid"]))
        self._batch_write(deletes=deletes)
