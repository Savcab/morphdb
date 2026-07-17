/*
 * morphdb-stream.js — the live-query client (streaming spec §8).
 *
 * A self-contained watch() that swaps transport automatically: Server-Sent
 * Events where the backend supports them (GET / reports "streaming": true),
 * polling everywhere else (Lambda, a briefly-down backend). App code cannot
 * tell which transport is running except by its freshness.
 *
 *   const db = MorphDBStream("http://127.0.0.1:8787", "my-app");
 *   const stop = db.watch("task", { where: { done: false }, sort: "priority" },
 *       ({ objects, added, updated, removed, initial }) => render(objects));
 *
 * The full morphdb.js SDK (separate spec) will absorb this file verbatim into
 * its reserved watch() seam; it lives standalone here to demonstrate the wire.
 *
 * The pure query helpers (comparator, delta application, backoff) are exported
 * for Node so they can be unit-tested; the browser ignores that branch.
 */
(function (root) {
  "use strict";

  // --- pure helpers (unit-tested) -------------------------------------------

  // Compare (present, value) the way the server's list path does: absent sorts
  // first ascending; the WHOLE comparison reverses for desc, so absent sorts
  // last descending. Datetimes are the server's normalized fixed-width ISO
  // strings and compare lexically — never Date-parsed.
  function cmpPresentValue(x, y) {
    var xp = x !== null && x !== undefined;
    var yp = y !== null && y !== undefined;
    if (xp !== yp) return xp ? 1 : -1;   // present after absent (ascending)
    if (!xp) return 0;                   // both absent
    if (x < y) return -1;
    if (x > y) return 1;
    return 0;
  }

  // The exact server comparator. Primary key reverses for desc; the _guid
  // tiebreak is ALWAYS ascending (the server pre-sorts by guid and leans on
  // sort stability — reversing the tiebreak too would diverge).
  function makeComparator(sort, order) {
    var desc = order === "desc";
    var key = sort || "_created_at";
    return function (a, b) {
      var c = cmpPresentValue(a[key], b[key]);
      if (desc) c = -c;
      if (c !== 0) return c;
      return a._guid < b._guid ? -1 : a._guid > b._guid ? 1 : 0;
    };
  }

  // Apply one delta event to the local result set, returning a fresh array.
  // enter/update upsert then re-sort (a sort-field change reorders); leave drops.
  function applyDelta(objects, event, payload, cmp) {
    if (event === "leave") {
      return objects.filter(function (x) { return x._guid !== payload.guid; });
    }
    var o = payload.object;
    var arr = objects.slice();
    var i = arr.findIndex(function (x) { return x._guid === o._guid; });
    if (i >= 0) arr[i] = o; else arr.push(o);
    arr.sort(cmp);
    return arr;
  }

  // Full-jitter exponential backoff: random(0, min(30s, 1s * 2^n)).
  function backoffDelay(n, rnd) {
    var r = (rnd || Math.random)();
    return Math.floor(r * Math.min(30000, 1000 * Math.pow(2, n)));
  }

  // Diff two result sets by _guid + _updated_at, for the polling/snapshot path,
  // so the callback conveniences are the same shape as delta's event translation.
  function diffByGuid(prev, next) {
    var prevById = {}, nextById = {};
    prev.forEach(function (o) { prevById[o._guid] = o; });
    next.forEach(function (o) { nextById[o._guid] = o; });
    var added = [], updated = [], removed = [];
    next.forEach(function (o) {
      var p = prevById[o._guid];
      if (!p) added.push(o);
      else if (p._updated_at !== o._updated_at) updated.push(o);
    });
    prev.forEach(function (o) { if (!nextById[o._guid]) removed.push(o); });
    return { added: added, updated: updated, removed: removed };
  }

  // Emit a query's filters in explicit-operator spelling so a field named like a
  // reserved control param (mode/refresh/app_key/limit/…) can never collide.
  function encodeWhere(where) {
    var parts = [];
    Object.keys(where || {}).forEach(function (k) {
      var v = where[k];
      var key = k.indexOf("__") >= 0 ? k : k + "__eq";
      if (Array.isArray(v)) v = v.join(",");
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(v));
    });
    return parts;
  }

  var PURE = {
    cmpPresentValue: cmpPresentValue,
    makeComparator: makeComparator,
    applyDelta: applyDelta,
    backoffDelay: backoffDelay,
    diffByGuid: diffByGuid,
    encodeWhere: encodeWhere,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = PURE;                 // Node: expose pure helpers for tests
    return;
  }

  // --- browser client -------------------------------------------------------

  function MorphDBStream(baseUrl, appKey) {
    baseUrl = (baseUrl || "").replace(/\/$/, "");
    var streamingCap = null;               // null=unknown, true/false once probed

    function url(path) { return baseUrl + path; }

    function listUrl(type, query, limit, offset) {
      var parts = encodeWhere(query.where);
      if (query.sort) parts.push("sort=" + encodeURIComponent(query.sort));
      if (query.order) parts.push("order=" + encodeURIComponent(query.order));
      if (limit != null) parts.push("limit=" + limit);
      if (offset != null) parts.push("offset=" + offset);
      return url("/objects/" + encodeURIComponent(type) + "?" + parts.join("&"));
    }

    function streamUrl(type, query, mode) {
      var parts = encodeWhere(query.where);
      if (query.sort) parts.push("sort=" + encodeURIComponent(query.sort));
      if (query.order) parts.push("order=" + encodeURIComponent(query.order));
      if (mode) parts.push("mode=" + mode);
      if (query.limit != null) parts.push("limit=" + query.limit);
      parts.push("app_key=" + encodeURIComponent(appKey));
      return url("/stream/" + encodeURIComponent(type) + "?" + parts.join("&"));
    }

    function probe() {
      return fetch(url("/")).then(function (r) { return r.json(); })
        .then(function (j) { streamingCap = !!j.streaming; return streamingCap; });
    }

    // Fetch the whole result by paginating the list GET to total — so a watch
    // with no limit means the whole result on the polling transport too, not the
    // server's default first page.
    function fetchAll(type, query) {
      function get(limit, offset) {
        return fetch(listUrl(type, query, limit, offset), {
          headers: { "X-App-Key": appKey },
        }).then(function (r) {
          if (!r.ok) return r.json().then(function (e) { throw e; });
          return r.json();
        });
      }
      // A windowed watch is exactly the server's page — identical to the
      // snapshot/stream result, not a paginated everything.
      if (query.limit != null) {
        return get(query.limit, query.offset || 0)
          .then(function (res) { return res.objects; });
      }
      // Unwindowed: the whole result, paginated to total.
      var acc = [], offset = 0, page = 500;
      function step() {
        return get(page, offset).then(function (res) {
          acc = acc.concat(res.objects);
          offset += res.objects.length;
          if (offset < res.total && res.objects.length) return step();
          return acc;
        });
      }
      return step();
    }

    function watch(type, query, cb) {
      query = query || {};
      var cmp = makeComparator(query.sort, query.order || "asc");
      var windowed = query.limit != null;      // a windowed watch → snapshot/poll
      var objects = [];
      var stopped = false;
      var es = null;
      var pollTimer = null;
      var attempt = 0;
      var everDelivered = false;

      function deliver(next, initial, conv) {
        var d = conv || diffByGuid(objects, next);
        objects = next;
        everDelivered = true;
        cb({ objects: objects, added: d.added, updated: d.updated,
             removed: d.removed, initial: !!initial });
      }

      // Query-shaped failure (an end frame, or a 4xx the poll would hit too):
      // surface it and stop this watcher for good — visibility must not revive it.
      function fail(err) {
        teardown();
        if (cb.onError) cb.onError(err);
      }

      // -- polling transport --
      function pollOnce() {
        return fetchAll(type, query).then(function (next) {
          if (stopped) return;
          if (!windowed) next = next.slice().sort(cmp);
          deliver(next, !everDelivered);   // only the first delivery is initial
        });
      }
      function startPolling(reprobe) {
        stopPolling();
        function schedule() {
          if (!stopped) pollTimer = setTimeout(loop, 2000);
        }
        function loop() {
          if (stopped) return;
          pollOnce().catch(function (e) {
            if (cb.onError) cb.onError(e);
          }).then(function () {
            if (stopped) return;
            // If we're only polling because streaming was transiently down,
            // re-probe; on success upgrade to the stream and DO NOT reschedule
            // the poll loop (else stream + poll would both feed deliver()).
            if (reprobe && streamingCap !== false) {
              probe().then(function (on) {
                if (stopped) return;
                if (on) connectStream();     // upgrades; connectStream stops polling
                else schedule();
              }).catch(schedule);
            } else {
              schedule();
            }
          });
        }
        loop();
      }
      function stopPolling() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      }

      // -- streaming transport --
      function connectStream() {
        stopPolling();                       // never run both transports at once
        var mode = windowed ? "snapshot" : "delta";
        openES(mode);
      }

      function openES(mode) {
        var src = new EventSource(streamUrl(type, query, mode));
        es = src;
        src.addEventListener("init", function (e) {
          attempt = 0;
          var data = JSON.parse(e.data);
          var next = data.objects.slice();
          if (mode === "delta") next.sort(cmp);
          deliver(next, true);
        });
        src.addEventListener("snapshot", function (e) {
          var next = JSON.parse(e.data).objects;
          deliver(next, false);
        });
        src.addEventListener("enter", function (e) {
          deliver(applyDelta(objects, "enter", JSON.parse(e.data), cmp), false,
                  { added: [JSON.parse(e.data).object], updated: [], removed: [] });
        });
        src.addEventListener("update", function (e) {
          deliver(applyDelta(objects, "update", JSON.parse(e.data), cmp), false,
                  { added: [], updated: [JSON.parse(e.data).object], removed: [] });
        });
        src.addEventListener("leave", function (e) {
          var guid = JSON.parse(e.data).guid;
          var gone = objects.filter(function (o) { return o._guid === guid; });
          deliver(applyDelta(objects, "leave", { guid: guid }, cmp), false,
                  { added: [], updated: [], removed: gone });
        });
        src.addEventListener("end", function (e) {
          // The server says this stream can't continue truthfully — the same
          // query would fail identically as a poll, so surface and stop for good.
          fail(JSON.parse(e.data).error);
        });
        src.onerror = function () {
          // EventSource exposes neither status nor body. Replay the attach once
          // as a fetch to classify: a query-shaped 4xx stops; anything else is
          // transport-shaped → poll in the interim and re-probe with backoff.
          closeES();
          classifyAndRecover(mode);
        };
      }

      function classifyAndRecover(mode) {
        if (stopped) return;
        fetch(streamUrl(type, query, mode), {
          headers: { Accept: "text/event-stream" },
        }).then(function (r) {
          var ct = r.headers.get("content-type") || "";
          if (r.ok && ct.indexOf("text/event-stream") >= 0) {
            // Healed transient — abort and resume streaming, don't hold the read.
            if (r.body && r.body.cancel) r.body.cancel();
            reconnectStream();
            return;
          }
          return r.json().then(function (err) {
            var code = (err && err.error && err.error.code) || "";
            if (mode === "delta" &&
                /include|limit|offset|delta/.test(err.error.message || "")) {
              windowed = true;               // eligibility-shaped → retry snapshot
              reconnectStream();
            } else if (r.status >= 400 && r.status < 500 && r.status !== 429) {
              fail(err.error || err);        // query-shaped → stop for good
            } else {
              startPolling(true);            // transport-shaped → poll + re-probe
            }
          });
        }).catch(function () {
          startPolling(true);                // network flap → poll + re-probe
        });
      }

      function reconnectStream() {
        if (stopped) return;
        var wait = backoffDelay(attempt++);
        setTimeout(function () { if (!stopped) connectStream(); }, wait);
      }

      function closeES() {
        if (es) { es.close(); es = null; }
      }

      // -- visibility: tear down hidden, re-attach on return (like polling) --
      function onVisibility() {
        if (typeof document === "undefined") return;
        if (document.hidden) { closeES(); stopPolling(); }
        else if (!stopped) start();
      }
      if (typeof document !== "undefined")
        document.addEventListener("visibilitychange", onVisibility);

      function start() {
        (streamingCap === null ? probe() : Promise.resolve(streamingCap))
          .then(function (on) {
            if (stopped) return;
            if (on) connectStream();
            else startPolling(false);        // streaming:false → poll for session
          })
          .catch(function () { if (!stopped) startPolling(true); });
      }

      function teardown() {
        stopped = true;
        closeES();
        stopPolling();
        if (typeof document !== "undefined")
          document.removeEventListener("visibilitychange", onVisibility);
      }

      start();

      return teardown;   // caller's stop()
    }

    return { watch: watch, _pure: PURE };
  }

  root.MorphDBStream = MorphDBStream;
})(typeof self !== "undefined" ? self : this);
