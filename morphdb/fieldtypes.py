"""Field type system for object schemas.

A schema field is normalized to ``{"type": <type>, "required": bool, "default": <any>}``.
Values are coerced/validated on write so that what comes back out of the store is
predictable for the frontend, even though the underlying storage is a JSON blob.

The type set is intentionally small and forgiving — coding agents generate messy
data and we would rather coerce than reject when the intent is unambiguous.
"""

import math
import re
from datetime import datetime

from .errors import bad_request

FIELD_TYPES = {"string", "number", "boolean", "json", "datetime"}

# Field names must be safe SQL/JSON identifiers: they are interpolated into
# json_extract paths (e.g. "$.title") and ORDER BY clauses, so restricting them
# to this charset closes any injection vector at the source.
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def normalize_field_def(name, raw):
    """Accept shorthand (``"string"``) or rich (``{"type": ...}``) field defs.

    Returns the canonical ``{"type", "required", "default"}`` form.
    """
    if isinstance(raw, str):
        ftype, required, default = raw, False, None
    elif isinstance(raw, dict):
        ftype = raw.get("type")
        required = bool(raw.get("required", False))
        default = raw.get("default")
    else:
        raise bad_request(
            f"Field '{name}' must be a type string or an object, got {type(raw).__name__}."
        )

    if ftype not in FIELD_TYPES:
        raise bad_request(
            f"Field '{name}' has unknown type '{ftype}'. "
            f"Valid types: {sorted(FIELD_TYPES)}."
        )

    # Validate the default eagerly so a bad default is caught at schema-define time.
    if default is not None:
        default = coerce_value(name, default, ftype)

    return {"type": ftype, "required": required, "default": default}


def normalize_fields(fields):
    """Normalize a whole ``{name: def}`` mapping. Validates field names."""
    if not isinstance(fields, dict):
        raise bad_request("'fields' must be an object mapping field name -> type.")
    out = {}
    for name, raw in fields.items():
        if not isinstance(name, str) or not name:
            raise bad_request(f"Field name must be a non-empty string, got {name!r}.")
        if name.startswith("_"):
            raise bad_request(
                f"Field name '{name}' is reserved (leading underscore is reserved "
                "for system fields like _guid/_type)."
            )
        if not _FIELD_NAME_RE.match(name):
            raise bad_request(
                f"Invalid field name '{name}'. Use a letter followed by letters, "
                "digits, or underscores (e.g. 'title', 'due_date')."
            )
        out[name] = normalize_field_def(name, raw)
    return out


def _parse_datetime(field, value):
    """Validate a datetime string and return it in ISO-8601 form, or raise."""
    s = value.strip()
    if not s:
        raise bad_request(f"Field '{field}': empty datetime string.")
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        datetime.fromisoformat(iso)
        return s  # already valid ISO-8601; keep caller's form
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    raise bad_request(
        f"Field '{field}': '{value}' is not a valid date/datetime (use ISO-8601)."
    )


def coerce_value(field, value, ftype):
    """Coerce a single value to its declared type, or raise ApiError.

    ``None`` always passes through (absence of a value is allowed unless the
    field is required, which is checked separately).
    """
    if value is None:
        return None

    if ftype == "string":
        if isinstance(value, (dict, list)):
            raise bad_request(f"Field '{field}' expects a string, got {type(value).__name__}.")
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    if ftype == "number":
        # bool is a subclass of int in Python — exclude it explicitly.
        if isinstance(value, bool):
            raise bad_request(f"Field '{field}' expects a number, got boolean.")
        if isinstance(value, (int, float)):
            num = value
        elif isinstance(value, str):
            try:
                f = float(value)
            except ValueError:
                raise bad_request(f"Field '{field}' expects a number, got {value!r}.")
            num = (int(f) if f.is_integer() and "." not in value
                   and "e" not in value.lower() else f)
        else:
            raise bad_request(
                f"Field '{field}' expects a number, got {type(value).__name__}."
            )
        # Reject non-finite values: json.dumps would emit bare NaN/Infinity
        # (invalid JSON) and SQLite's json_extract chokes on them.
        if isinstance(num, float) and not math.isfinite(num):
            raise bad_request(
                f"Field '{field}' must be a finite number (got {value!r})."
            )
        return num

    if ftype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "y", "on"):
                return True
            if low in ("false", "0", "no", "n", "off"):
                return False
        raise bad_request(f"Field '{field}' expects a boolean, got {value!r}.")

    if ftype == "datetime":
        # Normalize to an ISO-8601 string. Accept epoch seconds or an ISO string;
        # reject values that are not real dates so the column stays sortable.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.utcfromtimestamp(value).isoformat() + "Z"
            except (OverflowError, OSError, ValueError):
                raise bad_request(
                    f"Field '{field}': epoch value {value!r} is out of range."
                )
        if isinstance(value, str):
            return _parse_datetime(field, value)
        raise bad_request(
            f"Field '{field}' expects a datetime, got {type(value).__name__}."
        )

    if ftype == "json":
        # Any JSON-serializable value is fine; the store will json.dumps it.
        return value

    raise bad_request(f"Field '{field}' has unhandled type '{ftype}'.")


def project_data(stored, fields):
    """Project a stored JSON blob through the *current* schema (lazy invalidation).

    Fields that no longer exist in the schema are dropped from the output.
    Fields that exist in the schema but are missing from the blob are filled
    with their default (or ``None``). This is what makes schema edits O(1):
    we never rewrite stored rows, we just reinterpret them on read.
    """
    out = {}
    for name, fdef in fields.items():
        if name in stored:
            value = stored[name]
            # Re-coerce to the field's *current* type so a read never returns a
            # value that violates the live schema (e.g. after a field is retyped).
            # Best-effort: if the old value cannot be coerced, fall back to the
            # default rather than leaking a wrongly-typed value.
            if value is not None and fdef["type"] != "json":
                try:
                    value = coerce_value(name, value, fdef["type"])
                except Exception:
                    value = fdef.get("default")
            out[name] = value
        else:
            out[name] = fdef.get("default")
    return out


def validate_against_schema(data, fields, partial=False):
    """Coerce/validate an incoming ``data`` dict against schema fields.

    Unknown fields (not in the schema) are rejected — this keeps the data
    honest and surfaces agent typos early. When ``partial`` is True (PATCH-like
    upsert of a subset), required-field checks are skipped.
    """
    if not isinstance(data, dict):
        raise bad_request("Object data must be a JSON object.")

    unknown = [k for k in data if k not in fields and not k.startswith("_")]
    if unknown:
        raise bad_request(
            f"Unknown field(s) {unknown}. Declared fields: {sorted(fields)}. "
            "Update the schema first, or remove the stray field."
        )

    out = {}
    for name, fdef in fields.items():
        if name in data:
            out[name] = coerce_value(name, data[name], fdef["type"])
        elif not partial:
            # On a full write, materialize the default into the stored blob so
            # that queries (which read the blob directly) agree with reads
            # (which project the blob). A missing required field with no default
            # is an error.
            if fdef.get("default") is not None:
                out[name] = fdef["default"]
            elif fdef.get("required"):
                raise bad_request(f"Required field '{name}' is missing.")
    return out
