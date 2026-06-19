"""Small shared helpers."""

import uuid
from datetime import datetime, timezone


def now_iso():
    """Current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_guid(object_type):
    """A globally unique id, prefixed with a slug of the type for readability.

    e.g. ``task_3f1c9a0b8e7d4f...``. The prefix is cosmetic; the uuid carries
    the uniqueness. Non-alphanumeric chars in the type are stripped.
    """
    slug = "".join(c for c in str(object_type).lower() if c.isalnum())[:16] or "obj"
    return f"{slug}_{uuid.uuid4().hex}"
