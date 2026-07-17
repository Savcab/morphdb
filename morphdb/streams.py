"""Live queries — the streaming engine (spec: specs/streaming.html §6).

A subscription is a query kept true over time. This module owns everything
stream-shaped: the subscription registry, the bounded change bus, the two
daemon workers (dispatcher + refresh executor), the debounce loop, query-hash
coalescing, the resource caps, and the SSE frame encoding. The HTTP transport
(server.py) and the delta evaluator (PR4) plug in around it.

Threading model, two locks, one legal order (storage -> registry):
  * the storage lock (db._LOCK) serializes engine access and stamps the change
    seq; attaches and worker reads take it.
  * the registry lock (below) guards the subscription tables; workers copy what
    they need under it, release it, then touch storage.

This PR ships snapshot mode end to end (spec P1). Delta mode (membership sets,
enter/update/leave, the matcher seed, morph freeze/re-seed) is P2 and attaches
with mode="delta" are rejected until then.
"""

import collections
import json
import os
import threading
import time

from . import db
from . import objects as objs
from . import associations as assoc
from .errors import ApiError
from .schema import get_object_schema


# --- knobs (env-tunable, re-read on reset for fast tests) ---------------------

class _Knobs:
    def load(self):
        g = os.environ.get
        self.heartbeat = float(g("MORPHDB_STREAM_HEARTBEAT", "20"))
        self.queue_events = int(g("MORPHDB_STREAM_QUEUE_EVENTS", "64"))
        self.queue_bytes = int(g("MORPHDB_STREAM_QUEUE_BYTES", str(1024 * 1024)))
        self.bus_size = int(g("MORPHDB_STREAM_BUS_SIZE", "10000"))
        self.send_timeout = float(g("MORPHDB_STREAM_SEND_TIMEOUT", "20"))
        self.member_cap = int(g("MORPHDB_STREAM_MEMBER_CAP", "10000"))
        self.app_cap = int(g("MORPHDB_STREAM_APP_CAP", "100"))
        self.proc_cap = int(g("MORPHDB_STREAM_PROC_CAP", "500"))


KNOBS = _Knobs()
KNOBS.load()

# Backend-aware refresh defaults/bounds (ms). DynamoDB reads are dearer.
_REFRESH_DEFAULTS = {"dynamodb": 1000}
_REFRESH_DEFAULT = 200
_REFRESH_BOUNDS = {"dynamodb": (500, 60000)}
_REFRESH_BOUND = (50, 60000)

# Transport capability flag: serve() flips it true at startup so the
# request/response dispatch (Lambda, embedders) honestly reports streaming:false.
STREAMING = False


# --- SSE framing --------------------------------------------------------------

def encode_frame(event, data, seq):
    """One SSE frame: id + event + a single JSON data line, blank-terminated."""
    payload = json.dumps(data, default=str, allow_nan=False)
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n".encode("utf-8")


HEARTBEAT_FRAME = b": hb\n\n"
_RETRY_FRAME = b"retry: 3000\n\n"


# --- the subscription ---------------------------------------------------------

# Sentinels returned by Subscription.next_frame to the SSE writer.
HEARTBEAT = object()
CLOSED = object()

_Frame = collections.namedtuple("_Frame", ("event", "data"))


class Subscription:
    """One open stream. Owns the per-connection queue and seq counter; the SSE
    writer thread drains it via next_frame(), stamping seq at flush time so a
    collapsed/cleared queue never burns a seq number (the no-gap invariant)."""

    def __init__(self, app, object_type, filters, limit, offset, sort, order,
                 include, mode, refresh, qhash, trigger_types):
        self.app = app
        self.object_type = object_type
        self.filters = filters
        self.limit = limit
        self.offset = offset
        self.sort = sort
        self.order = order
        self.include = include
        self.mode = mode
        self.refresh = refresh          # effective debounce (ms), this sub's ask
        self.qhash = qhash
        self.trigger_types = trigger_types
        self.group = None
        self.fence = 0                  # this sub's own attach fence (§5.2)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue = collections.deque()
        self._bytes = 0
        self._seq = 0
        self._closed = False
        self._want_init = False         # next frame re-anchors as init (collapse)

    # -- writer-facing (SSE thread) -------------------------------------------

    def next_frame(self, timeout):
        """Block up to ``timeout`` s for the next frame. Returns HEARTBEAT on
        timeout, CLOSED once the stream is done, else (bytes, is_terminal)."""
        with self._cond:
            while not self._queue and not self._closed:
                if not self._cond.wait(timeout):
                    return HEARTBEAT, False
            if self._queue:
                frame = self._queue.popleft()
                self._bytes -= _frame_size(frame)
                self._seq += 1
                terminal = frame.event == "end"
                return encode_frame(frame.event, frame.data, self._seq), terminal
            return CLOSED, True

    # -- producer-facing (workers / attach) -----------------------------------

    def emit(self, event, data):
        """Enqueue a frame. On overflow, clear the queue and arm a fresh init:
        the stream self-heals to truth instead of buffering unboundedly."""
        with self._cond:
            self._enqueue(event, data)
            self._cond.notify()

    def _enqueue(self, event, data):
        """Append one frame under the lock. An init re-anchors: it replaces the
        whole queue and is never itself overflow-dropped (however large the seed,
        dropping the init would livelock the re-seed)."""
        if self._closed:
            return
        frame = _Frame(event, data)
        if event == "init":
            self._queue.clear()
            self._queue.append(frame)
            self._bytes = _frame_size(frame)
            self._want_init = False
            return
        self._queue.append(frame)
        self._bytes += _frame_size(frame)
        if (len(self._queue) > KNOBS.queue_events
                or self._bytes > KNOBS.queue_bytes):
            self._queue.clear()
            self._bytes = 0
            self._want_init = True
            if self.group is not None:
                self.group.mark_dirty()   # executor re-inits this sub

    def emit_init(self, result):
        # Clear want_init and enqueue the init atomically, so no concurrent frame
        # can slip between and land after the seed (leaving the client stale).
        with self._cond:
            self._enqueue("init", _init_payload(self.mode, result))
            self._cond.notify()

    def emit_result(self, result):
        """A group refresh: init if this sub is collapsing, else snapshot.
        Atomic so the collapse check and the enqueue can't race a concurrent
        emit."""
        with self._cond:
            if self._want_init:
                self._enqueue("init", _init_payload(self.mode, result))
            else:
                self._enqueue("snapshot", _result_payload(result))
            self._cond.notify()

    def end(self, code, message):
        with self._cond:
            if self._closed:
                return
            self._queue.append(_Frame("end", {"error": {"code": code,
                                                         "message": message}}))
            self._closed = True
            self._cond.notify()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify()

    @property
    def closed(self):
        return self._closed


def _frame_size(frame):
    if frame.event == "end":
        return 0
    return len(json.dumps(frame.data, default=str, allow_nan=False))


def _result_payload(result):
    out = {"objects": result["objects"], "total": result["total"]}
    if "limit" in result:
        out["limit"] = result["limit"]
        out["offset"] = result["offset"]
    return out


def _init_payload(mode, result):
    out = _result_payload(result)
    out["mode"] = mode
    return out


# --- query groups (coalescing) ------------------------------------------------

class Group:
    """All subscribers of one canonicalized query. One re-run per debounce
    window, one result, fanned to every subscriber (§6.3)."""

    def __init__(self, qhash, app, object_type, filters, limit, offset, sort,
                 order, include, mode, trigger_types, fence):
        self.qhash = qhash
        self.app = app
        self.object_type = object_type
        self.filters = filters
        self.limit = limit
        self.offset = offset
        self.sort = sort
        self.order = order
        self.include = include
        self.mode = mode
        self.trigger_types = trigger_types
        self.fence = fence
        self.subs = set()
        # debounce state, owned by the refresh executor
        self.deadline = None
        self.last_run = None
        self.dirty = False

    def min_refresh(self):
        return min((s.refresh for s in self.subs), default=_REFRESH_DEFAULT)

    def mark_dirty(self):
        _EXECUTOR.mark(self)


# --- module state -------------------------------------------------------------

_REG_LOCK = threading.RLock()
_GROUPS = {}                    # qhash -> Group
_BY_TRIGGER = {}               # (app, type) -> set(qhash)   dispatch routing
_TRIGGER_COUNT = {}           # (app, type) -> int          interested() index
_APP_COUNT = {}               # app -> int                  per-app cap
_TOTAL = 0                     # process-wide stream count


# --- interest gate (write-path) -----------------------------------------------

def interested(app, types):
    """Does any live subscription care about a write to ``types`` in ``app``?

    Empty ``types`` means an app-level op (delete_app): true iff the app has any
    stream. Cheap: dict probes against the trigger index.
    """
    with _REG_LOCK:
        if not types:
            return _APP_COUNT.get(app, 0) > 0
        return any((app, t) in _TRIGGER_COUNT for t in types)


# --- change bus + workers -----------------------------------------------------

class _ChangeBus:
    """Bounded FIFO of change records. On overflow the oldest record is dropped
    and its addresses (own type + touched types) collapse the affected groups
    (§6.2). schema_op records are never evicted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._q = collections.deque()
        self._stop = False

    def publish(self, records):
        with self._cond:
            for r in records:
                self._q.append(r)
            self._evict_locked()
            self._cond.notify()

    def _evict_locked(self):
        while len(self._q) > KNOBS.bus_size:
            # Drop the oldest evictable (non-schema_op) record.
            victim = None
            for i, r in enumerate(self._q):
                if "schema_op" not in r:
                    victim = i
                    break
            if victim is None:
                break                    # only schema_ops remain; keep them all
            r = self._q[victim]
            del self._q[victim]
            _DROPPED.append(r)

    def drain(self, timeout):
        with self._cond:
            if not self._q and not self._stop:
                self._cond.wait(timeout)
            batch = list(self._q)
            self._q.clear()
            return batch

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()


_DROPPED = collections.deque()   # records evicted from the bus, drained by dispatcher
_BUS = _ChangeBus()


class _Dispatcher(threading.Thread):
    """Drains the bus in seq order and routes each record. Snapshot: mark every
    group whose trigger set the record touches dirty. schema_op: recompute
    triggers / end illegal streams. Delta routing is added in PR4."""

    def __init__(self):
        super().__init__(daemon=True, name="morphdb-stream-dispatch")
        self._running = True

    def run(self):
        while self._running:
            batch = _BUS.drain(0.5)
            # Bus-overflow collapses first: a dropped record's transition is lost.
            while _DROPPED:
                self._collapse_dropped(_DROPPED.popleft())
            for record in batch:
                try:
                    self._route(record)
                except Exception:       # a bad record must not kill the worker
                    import traceback
                    traceback.print_exc()

    def stop(self):
        self._running = False
        _BUS.stop()

    def _route(self, record):
        if "schema_op" in record:
            self._route_schema_op(record)
            return
        if "dirty" in record:
            # Synthetic rollback-heal record: no body, just [app, type] addresses
            # to refresh (db stages this when a transaction rolls back after
            # staging, so a lock-free read of transient state self-heals).
            self._dirty_addresses({tuple(a) for a in record["dirty"]})
            return
        app = record["app"]
        addrs = {(app, record["type"])}
        for t, _g in record.get("touched", []):
            addrs.add((app, t))
        for qh in self._groups_for(addrs):
            g = _GROUPS.get(qh)
            if g is None or g.fence >= record["seq"]:
                continue
            if g.mode == "snapshot":
                g.mark_dirty()
            # delta routing: PR4

    def _dirty_addresses(self, addrs):
        for qh in self._groups_for(addrs):
            g = _GROUPS.get(qh)
            if g is not None and g.mode == "snapshot":
                g.mark_dirty()

    def _route_schema_op(self, record):
        app = record["app"]
        op = record["schema_op"]
        affected = op.get("affected_types", [])
        if op["op"] == "delete_app":
            for g in self._groups_of_app(app):
                self._end_group(g, "app_deleted", f"App '{app}' was deleted.")
            return
        if op["op"] == "delete_type":
            dtype = op.get("type")
            for g in list(self._groups_for({(app, t) for t in affected})):
                grp = _GROUPS.get(g)
                if grp is None:
                    continue
                if grp.object_type == dtype:
                    self._end_group(grp, "type_deleted",
                                    f"Type '{dtype}' was deleted.")
                else:
                    self._remorph_group(grp)
            return
        # morph: recompute triggers + mark dirty (snapshot). Delta freeze: PR4.
        for g in list(self._groups_for({(app, t) for t in affected})):
            grp = _GROUPS.get(g)
            if grp is not None:
                self._remorph_group(grp)

    def _remorph_group(self, g):
        """Recompute a snapshot group's trigger set against the new schema and
        mark it dirty. If the query went illegal, end it on the next refresh."""
        try:
            new_triggers = _compute_triggers(
                g.app, g.object_type, g.filters, g.include)
        except ApiError:
            # streamed type itself gone / query illegal at the structural level
            self._end_group(g, "query_illegal",
                            "The schema changed and this query is no longer legal.")
            return
        with _REG_LOCK:
            _reindex_triggers(g, new_triggers)
            g.trigger_types = new_triggers
        if g.mode == "snapshot":
            g.mark_dirty()

    def _end_group(self, g, code, message):
        with _REG_LOCK:
            subs = list(g.subs)
        for s in subs:
            s.end(code, message)
            _detach(s)

    def _collapse_dropped(self, record):
        app = record.get("app")
        addrs = set()
        if "schema_op" in record:
            for t in record["schema_op"].get("affected_types", []):
                addrs.add((app, t))
        elif "dirty" in record:
            addrs = {tuple(a) for a in record["dirty"]}
        else:
            addrs.add((app, record["type"]))
            for t, _g in record.get("touched", []):
                addrs.add((app, t))
        for qh in self._groups_for(addrs):
            g = _GROUPS.get(qh)
            if g is not None and g.mode == "snapshot":
                g.mark_dirty()
            # delta collapse: PR4

    @staticmethod
    def _groups_for(addrs):
        with _REG_LOCK:
            out = set()
            for a in addrs:
                out |= _BY_TRIGGER.get(a, set())
            return out

    @staticmethod
    def _groups_of_app(app):
        with _REG_LOCK:
            return [g for g in _GROUPS.values() if g.app == app]


class _Executor(threading.Thread):
    """Earliest-deadline-first debounce loop. A group quiet for >= refresh runs
    immediately (leading edge); a busy one runs at its window deadline, re-arming
    if re-dirtied mid-run so the final state always ships."""

    def __init__(self):
        super().__init__(daemon=True, name="morphdb-stream-refresh")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._deadlines = {}            # group -> monotonic deadline
        self._running = True

    def mark(self, group):
        now = time.monotonic()
        with self._cond:
            if group in self._deadlines:
                group.dirty = True      # re-arm handled after the run
                return
            refresh_s = group.min_refresh() / 1000.0
            if group.last_run is None or now - group.last_run >= refresh_s:
                deadline = now           # leading edge
            else:
                deadline = group.last_run + refresh_s
            self._deadlines[group] = deadline
            group.dirty = True
            self._cond.notify()

    def run(self):
        while self._running:
            group = None
            with self._cond:
                if not self._deadlines:
                    self._cond.wait(0.5)
                else:
                    now = time.monotonic()
                    g, dl = min(self._deadlines.items(), key=lambda kv: kv[1])
                    if dl <= now:
                        del self._deadlines[g]
                        g.dirty = False
                        group = g
                    else:
                        self._cond.wait(min(dl - now, 0.5))
            if group is not None:
                self._run_group(group)

    def _run_group(self, group):
        with _REG_LOCK:
            if group.qhash not in _GROUPS:
                return                   # detached while queued
            subs = list(group.subs)
        if not subs:
            return
        try:
            result = _run_query(group)
        except ApiError as e:
            for s in subs:
                s.end(e.code if hasattr(e, "code") else "query_illegal", str(e))
                _detach(s)
            return
        except Exception:
            import traceback
            traceback.print_exc()
            self._rearm(group)           # one bounded retry via re-arm
            return
        group.last_run = time.monotonic()
        for s in subs:
            s.emit_result(result)
        # Re-arm if re-dirtied during the run (trailing edge).
        with self._cond:
            if group.dirty and group not in self._deadlines:
                self._deadlines[group] = group.last_run + group.min_refresh() / 1000.0
                self._cond.notify()

    def _rearm(self, group):
        with self._cond:
            if group not in self._deadlines:
                self._deadlines[group] = time.monotonic() + 0.5
                self._cond.notify()

    def drop(self, group):
        with self._cond:
            self._deadlines.pop(group, None)

    def stop(self):
        self._running = False
        with self._cond:
            self._cond.notify_all()


_DISPATCHER = None
_EXECUTOR = None


# --- query compilation / running ----------------------------------------------

def _canonical_hash(app, object_type, filters, limit, offset, sort, order,
                    include, mode, fields, rel_views):
    """Canonicalize a query to a coalescing key: compiled specs (not raw
    spellings), sorted; sort/order kept for snapshot (server-ordered) and dropped
    for delta (client re-sorts). refresh and app_key stay out."""
    specs = objs._filter_specs(app, object_type, filters, fields, rel_views)
    norm = []
    for kind, name, op, val in specs:
        if isinstance(val, list):
            val = sorted(str(v) for v in val)
        norm.append((kind, name, op, json.dumps(val, default=str, sort_keys=True)))
    norm.sort()
    parts = [app, object_type, mode, tuple(norm)]
    if mode == "snapshot":
        parts += [sort, order, limit, offset,
                  json.dumps(_norm_include(include), sort_keys=True)]
    return json.dumps(parts, default=str, sort_keys=True)


def _norm_include(include):
    if not include:
        return {}
    return objs._parse_include(include)


def _compute_triggers(app, object_type, filters, include):
    """The types a change must touch to affect this query: the streamed type,
    plus every type reachable through relation filters and include paths."""
    fields = get_object_schema(app, object_type, required=True)["fields"]
    rel_views = {v["key"]: v for v in assoc.relation_views(app, object_type)}
    triggers = {object_type}
    # relation filters -> neighbor types
    for key in filters:
        name = key.rsplit("__", 1)[0] if "__" in key else key
        v = rel_views.get(name)
        if v is not None:
            triggers.add(v["neighbor_type"])
    # include paths -> every type along the way
    _walk_include(app, object_type, _norm_include(include), triggers)
    return triggers


def _walk_include(app, object_type, tree, acc):
    if not tree:
        return
    rel_views = {v["key"]: v for v in assoc.relation_views(app, object_type)}
    for key, subtree in tree.items():
        v = rel_views.get(key)
        if v is None:
            continue
        nt = v["neighbor_type"]
        acc.add(nt)
        _walk_include(app, nt, subtree, acc)


def _run_query(group):
    return objs.list_objects(
        group.app, group.object_type, filters=group.filters,
        limit=group.limit, offset=group.offset, sort=group.sort,
        order=group.order, include=group.include)


def _refresh_bounds():
    eng = db.engine()
    name = eng.name if eng is not None else "sqlite"
    default = _REFRESH_DEFAULTS.get(name, _REFRESH_DEFAULT)
    lo, hi = _REFRESH_BOUNDS.get(name, _REFRESH_BOUND)
    return default, lo, hi


def resolve_refresh(raw):
    """Clamp/validate the refresh param; None -> backend default."""
    default, lo, hi = _refresh_bounds()
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        from .errors import bad_request
        raise bad_request("refresh must be an integer number of milliseconds.")
    return max(lo, min(hi, v))


# --- attach / detach ----------------------------------------------------------

def attach(app, object_type, filters=None, limit=objs.DEFAULT_LIMIT, offset=0,
           sort=None, order="asc", include=None, mode="snapshot", refresh=None):
    """Open a stream. Validates like the list endpoint, registers under the
    storage lock with the change-seq fence, seeds the init, and returns a
    Subscription whose queue the SSE writer drains. Raises ApiError on bad
    query, unknown app/type, or a crossed cap."""
    if mode not in ("snapshot", "delta"):
        from .errors import bad_request
        raise bad_request("mode must be 'snapshot' or 'delta'.")
    if mode == "delta":
        from .errors import bad_request
        raise bad_request("delta mode is not yet available; use mode=snapshot.")

    filters = filters or {}
    refresh_ms = resolve_refresh(refresh)

    # Validate + seed + register, all under the storage lock so the fence and the
    # seed read are linearized with writes (§5.1).
    with db.storage_lock():
        fields = get_object_schema(app, object_type, required=True)["fields"]
        rel_views = {v["key"]: v for v in assoc.relation_views(app, object_type)}
        # Reuse the list endpoint's validation by running the seed query; it
        # raises the same didactic 400s for bad filters/sort.
        result = objs.list_objects(
            app, object_type, filters=filters, limit=limit, offset=offset,
            sort=sort, order=order, include=include)
        qhash = _canonical_hash(app, object_type, filters, limit, offset, sort,
                                order, include, mode, fields, rel_views)
        triggers = _compute_triggers(app, object_type, filters, include)
        fence = db.current_change_seq()

        sub = Subscription(app, object_type, filters, limit, offset, sort, order,
                           include, mode, refresh_ms, qhash, triggers)
        _register(sub, fence)

    sub.emit_init(result)
    return sub


def _register(sub, fence):
    global _TOTAL
    sub.fence = fence
    with _REG_LOCK:
        if _TOTAL >= KNOBS.proc_cap:
            _too_many("This MorphDB process is at its stream cap "
                      f"({KNOBS.proc_cap}).")
        if _APP_COUNT.get(sub.app, 0) >= KNOBS.app_cap:
            _too_many(f"App '{sub.app}' is at its stream cap ({KNOBS.app_cap}).")

        group = _GROUPS.get(sub.qhash)
        if group is None:
            group = Group(sub.qhash, sub.app, sub.object_type, sub.filters,
                          sub.limit, sub.offset, sub.sort, sub.order,
                          sub.include, sub.mode, sub.trigger_types, fence)
            _GROUPS[sub.qhash] = group
            for t in sub.trigger_types:
                _BY_TRIGGER.setdefault((sub.app, t), set()).add(sub.qhash)
        group.subs.add(sub)
        sub.group = group

        for t in sub.trigger_types:
            key = (sub.app, t)
            _TRIGGER_COUNT[key] = _TRIGGER_COUNT.get(key, 0) + 1
        _APP_COUNT[sub.app] = _APP_COUNT.get(sub.app, 0) + 1
        _TOTAL += 1


def _too_many(message):
    raise ApiError(429, "too_many_streams", message)


def detach(sub):
    """Close a stream and release its registry slot."""
    sub.close()
    _detach(sub)


def _detach(sub):
    global _TOTAL
    with _REG_LOCK:
        group = sub.group
        if group is None or sub not in group.subs:
            return
        group.subs.discard(sub)
        for t in sub.trigger_types:
            key = (sub.app, t)
            n = _TRIGGER_COUNT.get(key, 0) - 1
            if n <= 0:
                _TRIGGER_COUNT.pop(key, None)
            else:
                _TRIGGER_COUNT[key] = n
        n = _APP_COUNT.get(sub.app, 0) - 1
        if n <= 0:
            _APP_COUNT.pop(sub.app, None)
        else:
            _APP_COUNT[sub.app] = n
        _TOTAL -= 1
        if not group.subs:
            _GROUPS.pop(group.qhash, None)
            for t in group.trigger_types:
                s = _BY_TRIGGER.get((group.app, t))
                if s is not None:
                    s.discard(group.qhash)
                    if not s:
                        _BY_TRIGGER.pop((group.app, t), None)
            if _EXECUTOR is not None:
                _EXECUTOR.drop(group)
        sub.group = None


def _reindex_triggers(group, new_triggers):
    """Swap a group's trigger addresses in both indexes (called under _REG_LOCK
    during a morph re-key)."""
    old = group.trigger_types
    n = len(group.subs)
    for t in old - new_triggers:
        _BY_TRIGGER.get((group.app, t), set()).discard(group.qhash)
        if not _BY_TRIGGER.get((group.app, t)):
            _BY_TRIGGER.pop((group.app, t), None)
        key = (group.app, t)
        c = _TRIGGER_COUNT.get(key, 0) - n
        if c <= 0:
            _TRIGGER_COUNT.pop(key, None)
        else:
            _TRIGGER_COUNT[key] = c
    for t in new_triggers - old:
        _BY_TRIGGER.setdefault((group.app, t), set()).add(group.qhash)
        _TRIGGER_COUNT[(group.app, t)] = _TRIGGER_COUNT.get((group.app, t), 0) + n
    # Keep each sub's own trigger set in lockstep with the group's — _detach
    # accounts _TRIGGER_COUNT off sub.trigger_types, so a stale copy would
    # double-decrement a trigger a sibling group still needs.
    for s in group.subs:
        s.trigger_types = new_triggers


# --- lifecycle ----------------------------------------------------------------

def _publish_consumer(records):
    _BUS.publish(records)


def start():
    """Start the workers and install the write-path publish hook. Idempotent."""
    global _DISPATCHER, _EXECUTOR
    if _DISPATCHER is not None:
        return
    KNOBS.load()
    _EXECUTOR = _Executor()
    _DISPATCHER = _Dispatcher()
    _EXECUTOR.start()
    _DISPATCHER.start()
    db.set_publish_hook(_publish_consumer, interested=interested)


def stop():
    """Stop the workers, close all streams, uninstall the hook."""
    global _DISPATCHER, _EXECUTOR, _TOTAL
    db.set_publish_hook(None)
    if _DISPATCHER is not None:
        _DISPATCHER.stop()
    if _EXECUTOR is not None:
        _EXECUTOR.stop()
    _DISPATCHER = None
    _EXECUTOR = None
    with _REG_LOCK:
        for g in list(_GROUPS.values()):
            for s in list(g.subs):
                s.close()
        _GROUPS.clear()
        _BY_TRIGGER.clear()
        _TRIGGER_COUNT.clear()
        _APP_COUNT.clear()
        _TOTAL = 0
    _DROPPED.clear()


def reset():
    """Test hook: tear down and restart clean, re-reading env knobs. The module
    state outlives db.init_db, so tests call this between cases."""
    stop()
    _BUS._q.clear()
    KNOBS.load()
    start()
