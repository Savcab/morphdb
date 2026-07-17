/*
 * Unit tests for the pure query helpers in morphdb-stream.js (the parts that
 * decide correctness without a browser). Run with: node stream.test.cjs
 * The Python suite shells out to this via tests/test_stream_client.py.
 */
"use strict";
const assert = require("assert");
const {
  cmpPresentValue, makeComparator, applyDelta, backoffDelay, diffByGuid,
  encodeWhere,
} = require("./morphdb-stream.js");

function obj(guid, extra) { return Object.assign({ _guid: guid }, extra); }

// --- the exact comparator, all three traps --------------------------------

// trap 1: nulls sort first ascending, last descending
{
  const asc = makeComparator("priority", "asc");
  const desc = makeComparator("priority", "desc");
  const withNull = obj("a", { priority: null });
  const withVal = obj("b", { priority: 5 });
  assert.ok(asc(withNull, withVal) < 0, "null first ascending");
  assert.ok(desc(withNull, withVal) > 0, "null last descending");
}

// trap 2: ties break by _guid ascending in BOTH directions
{
  const asc = makeComparator("priority", "asc");
  const desc = makeComparator("priority", "desc");
  const a = obj("guid_a", { priority: 5 });
  const b = obj("guid_b", { priority: 5 });
  assert.ok(asc(a, b) < 0, "guid asc tiebreak (asc)");
  assert.ok(desc(a, b) < 0, "guid asc tiebreak NOT reversed (desc)");
}

// trap 3: datetimes compare lexically as ISO strings, never Date-parsed
{
  const cmp = makeComparator("_created_at", "asc");
  const early = obj("a", { _created_at: "2026-01-01T00:00:00.000Z" });
  const late = obj("b", { _created_at: "2026-12-31T23:59:59.000Z" });
  assert.ok(cmp(early, late) < 0, "iso lexical order");
}

// default sort is _created_at
{
  const cmp = makeComparator(null, "asc");
  const a = obj("a", { _created_at: "2026-01-01T00:00:00.000Z" });
  const b = obj("b", { _created_at: "2026-02-01T00:00:00.000Z" });
  assert.ok(cmp(a, b) < 0);
}

// a full sort matches: reverse order, nulls to the end, stable guid ties
{
  const cmp = makeComparator("n", "desc");
  const rows = [
    obj("g3", { n: 1 }), obj("g1", { n: 5 }), obj("g2", { n: 5 }),
    obj("g4", { n: null }),
  ];
  const sorted = rows.slice().sort(cmp).map((r) => r._guid);
  assert.deepStrictEqual(sorted, ["g1", "g2", "g3", "g4"]);
}

// --- applyDelta ------------------------------------------------------------

{
  const cmp = makeComparator("n", "asc");
  let arr = [obj("g1", { n: 2 })];
  arr = applyDelta(arr, "enter", { object: obj("g2", { n: 1 }) }, cmp);
  assert.deepStrictEqual(arr.map((x) => x._guid), ["g2", "g1"], "enter re-sorts");

  arr = applyDelta(arr, "update", { object: obj("g2", { n: 9 }) }, cmp);
  assert.deepStrictEqual(arr.map((x) => x._guid), ["g1", "g2"],
    "update reorders on sort-field change");

  arr = applyDelta(arr, "leave", { guid: "g1" }, cmp);
  assert.deepStrictEqual(arr.map((x) => x._guid), ["g2"], "leave drops");

  // enter of an existing guid upserts, does not duplicate
  arr = applyDelta(arr, "enter", { object: obj("g2", { n: 3 }) }, cmp);
  assert.strictEqual(arr.length, 1, "enter upserts existing guid");
}

// --- backoff bounds --------------------------------------------------------

{
  assert.strictEqual(backoffDelay(0, () => 0), 0);
  assert.strictEqual(backoffDelay(0, () => 1), 1000);
  assert.strictEqual(backoffDelay(3, () => 1), 8000);
  assert.strictEqual(backoffDelay(20, () => 1), 30000, "capped at 30s");
}

// --- diffByGuid ------------------------------------------------------------

{
  const prev = [obj("a", { _updated_at: "1" }), obj("b", { _updated_at: "1" })];
  const next = [obj("a", { _updated_at: "1" }), obj("c", { _updated_at: "1" })];
  const d = diffByGuid(prev, next);
  assert.deepStrictEqual(d.added.map((x) => x._guid), ["c"]);
  assert.deepStrictEqual(d.removed.map((x) => x._guid), ["b"]);
  assert.strictEqual(d.updated.length, 0);

  const d2 = diffByGuid([obj("a", { _updated_at: "1" })],
                        [obj("a", { _updated_at: "2" })]);
  assert.deepStrictEqual(d2.updated.map((x) => x._guid), ["a"]);
}

// --- encodeWhere: explicit-operator spelling -------------------------------

{
  const parts = encodeWhere({ done: false, priority__gte: 3, tags: ["x", "y"] });
  assert.ok(parts.indexOf("done__eq=false") >= 0, "bare key gets __eq");
  assert.ok(parts.indexOf("priority__gte=3") >= 0, "explicit op preserved");
  assert.ok(parts.indexOf("tags__eq=x%2Cy") >= 0, "list joined");
}

console.log("morphdb-stream pure helpers: all assertions passed");
