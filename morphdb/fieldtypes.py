"""Field type system for object schemas.

A schema field is normalized to ``{"type": <type>, "required": bool, "default": <any>}``.
Values are coerced/validated on write so that what comes back out of the store is
predictable for the frontend, even though the underlying storage is a JSON blob.

The type set is intentionally small and forgiving — coding agents generate messy
data and we would rather coerce than reject when the intent is unambiguous.
"""

import math
import re
from datetime import datetime, timezone

from .errors import bad_request

FIELD_TYPES = {"string", "number", "boolean", "json", "datetime"}

# Field names must be safe SQL/JSON identifiers: they are interpolated into
# json_extract paths (e.g. "$.title") and ORDER BY clauses, so restricting them
# to this charset closes any injection vector at the source. Anchor with \Z (not
# $, which also matches just before a trailing newline) so "city\n" is rejected.
_FIELD_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")
_NUM_STR_RE = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?")
_INT_STR_RE = re.compile(r"-?\d+")

# A bare numeric datetime string is treated as epoch seconds only above this
# magnitude (~1973); smaller bare numbers like "2024" are ambiguous (a year?)
# and are rejected rather than silently parsed as a 1970-relative timestamp.
_EPOCH_MIN_ABS = 1e8

# Canonical datetime form: fixed-width UTC ISO-8601 so lexical ordering equals
# chronological ordering and all equivalent representations collapse to one.
_DT_CANON = "%Y-%m-%dT%H:%M:%S.%fZ"


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
        if "__" in name:
            # '__' is the filter operator separator (e.g. field__gt); a field
            # name containing it would be unqueryable, so forbid it.
            raise bad_request(
                f"Field name '{name}' may not contain '__' (reserved for filter "
                "operators like field__gt)."
            )
        out[name] = normalize_field_def(name, raw)
    return out


def _canonical_dt(dtobj):
    """Render a datetime as the canonical fixed-width UTC ISO string.

    Naive datetimes are assumed to be UTC; aware ones are converted to UTC.
    """
    if dtobj.tzinfo is None:
        dtobj = dtobj.replace(tzinfo=timezone.utc)
    return dtobj.astimezone(timezone.utc).strftime(_DT_CANON)


def _epoch_to_canonical(field, value):
    fval = float(value)
    # Reject ambiguously-small magnitudes consistently for both JSON numbers and
    # numeric strings (a value like 0 or 2024 is more likely a mistake than a
    # 1970-relative timestamp).
    if abs(fval) < _EPOCH_MIN_ABS:
        raise bad_request(
            f"Field '{field}': ambiguous datetime {value!r}; use an ISO-8601 "
            f"string, or epoch seconds with magnitude >= {int(_EPOCH_MIN_ABS)}."
        )
    try:
        return _canonical_dt(datetime.fromtimestamp(fval, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        raise bad_request(f"Field '{field}': epoch value {value!r} is out of range.")


def _parse_datetime(field, value):
    """Validate a datetime string and return it in canonical UTC ISO form.

    Accepts ISO-8601 (with or without 'Z'/offset), a few common formats, and a
    bare numeric string treated as epoch seconds (so query values can match the
    epoch-seconds write path).
    """
    s = value.strip()
    if not s:
        raise bad_request(f"Field '{field}': empty datetime string.")
    # Treat a bare number as epoch seconds only if it's large enough to be an
    # unambiguous timestamp; otherwise fall through (and likely 400) so a value
    # like "2024" isn't silently turned into a 1970 instant.
    if _NUM_STR_RE.fullmatch(s) and abs(float(s)) >= _EPOCH_MIN_ABS:
        return _epoch_to_canonical(field, s)
    # 'Z' (UTC) must be a single trailing marker and not coexist with another
    # offset — fromisoformat would silently ignore a misplaced/duplicate 'Z'.
    if "Z" in s:
        core = s[:-1]
        if s.count("Z") != 1 or not s.endswith("Z") or "+" in core \
                or core.count("-") > 2:
            raise bad_request(
                f"Field '{field}': malformed datetime '{value}'."
            )
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    # Python 3.10 fromisoformat accepts only 3/6-digit fractional seconds;
    # truncate longer (nano/micro) fractions to 6 digits so valid ISO-8601
    # timestamps from Go/Java/JS are accepted.
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
    try:
        return _canonical_dt(datetime.fromisoformat(iso))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return _canonical_dt(datetime.strptime(s, fmt))
        except ValueError:
            continue
    raise bad_request(
        f"Field '{field}': '{value}' is not a valid date/datetime (use ISO-8601)."
    )


def _reject_non_finite(field, value):
    """Recursively reject NaN/Infinity inside a json value.

    json.dumps would otherwise emit bare NaN/Infinity tokens (invalid JSON) that
    poison every later read of the row, so we forbid them at write time.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise bad_request(
            f"Field '{field}': json values may not contain NaN or Infinity."
        )
    if isinstance(value, dict):
        for v in value.values():
            _reject_non_finite(field, v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_non_finite(field, v)


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
            s = value.strip()
            if not _NUM_STR_RE.fullmatch(s):
                # Reject anything that isn't a plain decimal/exponent number,
                # including Python-only forms like underscore separators.
                raise bad_request(f"Field '{field}' expects a number, got {value!r}.")
            # Parse integer strings with int() (not float()) to preserve exact
            # value for magnitudes beyond float's 53-bit mantissa.
            num = int(s) if _INT_STR_RE.fullmatch(s) else float(s)
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
        # Normalize to a canonical UTC ISO string. Accept epoch seconds or an ISO
        # string; reject values that are not real dates so the column stays
        # sortable/comparable.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _epoch_to_canonical(field, value)
        if isinstance(value, str):
            return _parse_datetime(field, value)
        raise bad_request(
            f"Field '{field}' expects a datetime, got {type(value).__name__}."
        )

    if ftype == "json":
        # Any JSON-serializable value is fine, except non-finite floats which
        # are not valid JSON and would poison later reads.
        _reject_non_finite(field, value)
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
        # Stored values are returned exactly as written; the default fills in
        # only for a key that is absent from the blob (not for a stored null).
        # Querying mirrors this precisely (see objects._field_expr), so reads and
        # queries never disagree. Schema edits affect validation of future writes
        # only — existing values are not rewritten or reinterpreted.
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
