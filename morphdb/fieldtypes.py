"""Field type system for object schemas.

A schema field is normalized to ``{"type": <type>, "required": bool, "default": <any>}``.
Values are coerced/validated on write so that what comes back out of the store is
predictable for the frontend, even though the underlying storage is a JSON blob.

The type set is intentionally small and forgiving — coding agents generate messy
data and we would rather coerce than reject when the intent is unambiguous.
"""

from datetime import datetime

from .errors import bad_request

FIELD_TYPES = {"string", "number", "boolean", "json", "datetime"}


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
        out[name] = normalize_field_def(name, raw)
    return out


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
            return value
        if isinstance(value, str):
            try:
                f = float(value)
            except ValueError:
                raise bad_request(f"Field '{field}' expects a number, got {value!r}.")
            return int(f) if f.is_integer() and "." not in value and "e" not in value.lower() else f
        raise bad_request(f"Field '{field}' expects a number, got {type(value).__name__}.")

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
        # Store as an ISO-8601 string; accept datetime, epoch seconds, or a string.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.utcfromtimestamp(value).isoformat() + "Z"
        if isinstance(value, str):
            return value  # trust caller's ISO string; parsing is best-effort below
        raise bad_request(f"Field '{field}' expects a datetime string, got {type(value).__name__}.")

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
            out[name] = stored[name]
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
        elif not partial and fdef.get("required") and fdef.get("default") is None:
            raise bad_request(f"Required field '{name}' is missing.")
    return out
