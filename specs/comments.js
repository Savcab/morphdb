/* ---------------------------------------------------------------------------
 * MorphDB spec comments — text-anchored, Notion/GitHub style.
 *
 * Select any text in a spec → a floating "Comment" button appears → write a
 * comment anchored to THAT span. The span is highlighted; the comment shows in
 * the right margin (collapses under the section on narrow screens).
 *
 * Anchoring is 100% client-side (no new backend primitive). A comment stores the
 * quoted text + a little surrounding context + char offsets + a doc-version hash.
 * On load we re-find the span: try the offsets, else search by quote+context. If
 * the text changed so the quote is gone, the comment is shown as OUTDATED (kept,
 * with its original quote as context) — exactly how a GitHub review comment goes
 * stale when its line changes.
 *
 * Comments live in the CLOUD MorphDB app `morphdb-spec-comments` (hardcoded host,
 * no env vars), so every visitor to the public docs shares the same threads.
 * ------------------------------------------------------------------------- */
(() => {
  const CFG = window.SPEC_COMMENTS || {};
  const SLUG = CFG.slug || "spec";
  const HOST = CFG.host || "https://hjvvxxmlcrp6jzkfaohctk7cda0qshum.lambda-url.us-west-1.on.aws";
  const APP  = CFG.app  || "morphdb-spec-comments";
  const SECTION_SEL = CFG.sectionSelector || "main > section[id], main > header[id]";
  const NAME_KEY = "morphdb-spec:name";
  const CTX = 40;                      // chars of prefix/suffix context stored
  const HL_OK = !!(window.Highlight && window.CSS && CSS.highlights);

  // ---- backend -------------------------------------------------------------
  async function api(method, path, body) {
    const res = await fetch(HOST + path, {
      method,
      headers: { "Content-Type": "application/json", "X-App-Key": APP },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) throw new Error(data?.error?.message || `HTTP ${res.status}`);
    return data;
  }
  async function ensureBackend() {
    let exists = false;
    try { exists = (await fetch(HOST + "/schema/comment", { headers: { "X-App-Key": APP } })).ok; } catch (_) {}
    if (!exists) {
      await fetch(HOST + "/app", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: APP }) }).catch(() => {});
    }
    await api("PUT", "/schema/comment", {
      merge: true,
      fields: {
        spec: { type: "string", index: true }, section: { type: "string", index: true },
        author: { type: "string" }, body: { type: "string" },
        quote: { type: "string" }, prefix: { type: "string" }, suffix: { type: "string" },
        startOffset: { type: "number" }, endOffset: { type: "number" }, docVersion: { type: "string" },
      },
    });
  }

  // ---- helpers -------------------------------------------------------------
  function ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 2592000) return Math.floor(s / 86400) + "d ago";
    return new Date(iso).toLocaleDateString();
  }
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  function hash(s) { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
  const el = (tag, props) => Object.assign(document.createElement(tag), props || {});

  // Map of a section's *content* text nodes (skipping our injected UI), in order.
  function textMap(sec) {
    const nodes = [];
    const w = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        for (let p = n.parentElement; p && p !== sec; p = p.parentElement)
          if (p.hasAttribute && p.hasAttribute("data-mdb")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let total = 0, node;
    while ((node = w.nextNode())) { nodes.push({ node, start: total, len: node.nodeValue.length }); total += node.nodeValue.length; }
    return { nodes, text: nodes.map((x) => x.node.nodeValue).join(""), total };
  }
  // (container, offset) boundary → global char offset within the section's text map
  function pointToOffset(map, container, offset) {
    if (container.nodeType === 3) {
      const e = map.nodes.find((x) => x.node === container);
      return e ? e.start + offset : 0;
    }
    const b = document.createRange(); b.setStart(container, offset); b.collapse(true);
    let count = 0;
    for (const e of map.nodes) {
      let cmp; try { cmp = b.comparePoint(e.node, e.len); } catch (_) { cmp = 1; }
      if (cmp < 0) count += e.len; else break;        // node ends before the boundary
    }
    return count;
  }
  function locate(map, off) {
    for (const e of map.nodes) if (off <= e.start + e.len) return { node: e.node, local: Math.max(0, off - e.start) };
    const last = map.nodes[map.nodes.length - 1];
    return last ? { node: last.node, local: last.len } : null;
  }
  function rangeFromOffsets(map, start, end) {
    const a = locate(map, start), b = locate(map, end);
    if (!a || !b) return null;
    const r = document.createRange();
    try { r.setStart(a.node, a.local); r.setEnd(b.node, b.local); } catch (_) { return null; }
    return r;
  }
  const commonSuffix = (a, b) => { let i = 0; while (i < a.length && i < b.length && a[a.length - 1 - i] === b[b.length - 1 - i]) i++; return i; };
  const commonPrefix = (a, b) => { let i = 0; while (i < a.length && i < b.length && a[i] === b[i]) i++; return i; };
  // find the best occurrence of `quote`, disambiguated by surrounding context
  function quoteSearch(full, quote, prefix, suffix) {
    if (!quote) return -1;
    const idxs = []; let i = full.indexOf(quote);
    while (i >= 0) { idxs.push(i); i = full.indexOf(quote, i + 1); }
    if (idxs.length <= 1) return idxs.length ? idxs[0] : -1;
    let best = idxs[0], score = -1;
    for (const idx of idxs) {
      const pre = full.slice(Math.max(0, idx - (prefix ? prefix.length : 0)), idx);
      const suf = full.slice(idx + quote.length, idx + quote.length + (suffix ? suffix.length : 0));
      const sc = (prefix ? commonSuffix(pre, prefix) : 0) + (suffix ? commonPrefix(suf, suffix) : 0);
      if (sc > score) { score = sc; best = idx; }
    }
    return best;
  }
  // Re-anchor a stored comment within its section. Returns {range, start, end, outdated}.
  function anchor(map, c) {
    const full = map.text, q = c.quote || "";
    if (!q) return { range: null, start: 0, end: 0, outdated: true };
    if (typeof c.startOffset === "number" && full.slice(c.startOffset, c.endOffset) === q)
      return { range: rangeFromOffsets(map, c.startOffset, c.endOffset), start: c.startOffset, end: c.endOffset, outdated: false };
    const idx = quoteSearch(full, q, c.prefix, c.suffix);
    if (idx >= 0) return { range: rangeFromOffsets(map, idx, idx + q.length), start: idx, end: idx + q.length, outdated: false };
    return { range: null, start: 0, end: 0, outdated: true };
  }

  // ---- highlights (CSS Custom Highlight API — no DOM mutation) --------------
  const HL = HL_OK ? new Highlight() : null;
  const HLA = HL_OK ? new Highlight() : null;
  if (HL_OK) { CSS.highlights.set("mdb-comment", HL); CSS.highlights.set("mdb-active", HLA); }
  function refreshHighlights(entries) {
    if (!HL_OK) return;
    HL.clear();
    for (const e of entries) if (e.range && !e.outdated) HL.add(e.range);
  }
  function emphasize(range) { if (HL_OK) { HLA.clear(); if (range) HLA.add(range); } }

  // ---- styles --------------------------------------------------------------
  function injectCSS() {
    const css = `
    ::highlight(mdb-comment){background:rgba(231,178,76,.20);border-radius:2px}
    ::highlight(mdb-active){background:rgba(231,178,76,.45)}
    .mdb-sec{position:relative}
    .mdb-layer{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column;gap:8px}
    .mdb-card{background:var(--cyan-bg,rgba(115,184,208,.07));border:1px solid var(--line,#27424f);
      border-left:2px solid var(--brass,#E7B24C);border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.45;cursor:pointer}
    .mdb-card:hover{border-color:var(--brass,#E7B24C)}
    .mdb-card.outdated{border-left-color:var(--ink-3,#8aa0ad);opacity:.78}
    .mdb-quote{font-size:11.5px;color:var(--brass,#E7B24C);border-left:2px solid rgba(231,178,76,.4);
      padding-left:7px;margin-bottom:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .mdb-card.outdated .mdb-quote{color:var(--ink-3,#8aa0ad);text-decoration:line-through;border-left-color:var(--line,#27424f)}
    .mdb-meta{font-size:11px;color:var(--ink-3,#8aa0ad);margin-bottom:3px}
    .mdb-meta b{color:var(--ink,#ECE4D2);font-weight:600}
    .mdb-meta time{margin-left:6px}
    .mdb-badge{margin-left:7px;font-family:"IBM Plex Mono",monospace;font-size:9.5px;letter-spacing:.06em;
      text-transform:uppercase;color:var(--ink-3,#8aa0ad);border:1px solid var(--line,#27424f);border-radius:4px;padding:1px 5px}
    .mdb-cbody{color:var(--ink-2,#b9c6cf);white-space:pre-wrap;overflow-wrap:anywhere}
    /* floating selection button + compose popover (appended to body) */
    .mdb-selbtn{position:absolute;z-index:9998;transform:translate(-50%,-100%);font:inherit;font-size:12.5px;font-weight:600;
      cursor:pointer;background:#1b1206;color:var(--brass,#E7B24C);border:1px solid var(--brass-d,#b98a2f);border-radius:7px;
      padding:5px 11px;box-shadow:0 4px 16px rgba(0,0,0,.45);font-family:system-ui,sans-serif;white-space:nowrap}
    .mdb-pop{position:absolute;z-index:9999;width:280px;max-width:88vw;background:var(--ground-2,#0b1a26);
      border:1px solid var(--line,#27424f);border-radius:10px;padding:12px;box-shadow:0 8px 30px rgba(0,0,0,.5);
      font-family:system-ui,-apple-system,sans-serif}
    .mdb-pop .mdb-quote{margin-bottom:8px}
    .mdb-pop input,.mdb-pop textarea{font:inherit;font-size:13px;color:var(--ink,#ECE4D2);background:rgba(0,0,0,.28);
      border:1px solid var(--line,#27424f);border-radius:6px;padding:7px 9px;width:100%;margin-bottom:7px}
    .mdb-pop textarea{min-height:64px;resize:vertical}
    .mdb-pop input::placeholder,.mdb-pop textarea::placeholder{color:var(--ink-3,#8aa0ad)}
    .mdb-actions{display:flex;align-items:center;gap:8px}
    .mdb-actions button{font:inherit;font-size:12px;font-weight:600;cursor:pointer;border-radius:6px;padding:6px 13px}
    .mdb-post{background:var(--brass,#E7B24C);color:#1a1206;border:none}
    .mdb-post:disabled{opacity:.5;cursor:default}
    .mdb-cancel{background:transparent;color:var(--ink-3,#8aa0ad);border:1px solid var(--line,#27424f)}
    .mdb-msg{font-size:11px;color:var(--ink-3,#8aa0ad)}.mdb-msg.err{color:var(--deny,#e0664b)}
    .mdb-hint{font-family:"IBM Plex Mono",monospace;font-size:10px;color:var(--ink-3,#8aa0ad);margin-top:6px}
    @media (min-width:1200px){
      main{padding-right:248px}
      .mdb-layer{position:absolute;left:100%;margin-left:18px;top:2px;width:212px}
    }
    @media (max-width:1199px){ .mdb-layer{margin-top:10px;max-width:70ch} }`;
    document.head.appendChild(el("style", { textContent: css }));
  }

  // ---- card rendering ------------------------------------------------------
  function renderCard(entry) {
    const c = entry.comment;
    const card = el("div", { className: "mdb-card" + (entry.outdated ? " outdated" : "") });
    card.setAttribute("data-mdb", "");
    if (c.quote) {
      const q = el("div", { className: "mdb-quote", textContent: norm(c.quote) });
      card.appendChild(q);
    }
    const meta = el("div", { className: "mdb-meta" });
    meta.appendChild(el("b", { textContent: c.author || "Anonymous" }));
    if (c._created_at) { const t = el("time", { dateTime: c._created_at, textContent: "· " + ago(c._created_at) }); meta.appendChild(t); }
    if (entry.outdated) meta.appendChild(el("span", { className: "mdb-badge", textContent: "outdated" }));
    card.appendChild(meta);
    card.appendChild(el("div", { className: "mdb-cbody", textContent: c.body || "" }));
    card.addEventListener("mouseenter", () => emphasize(entry.range));
    card.addEventListener("mouseleave", () => emphasize(null));
    card.addEventListener("click", () => {
      if (entry.range) {
        const rect = entry.range.getBoundingClientRect();
        window.scrollTo({ top: window.scrollY + rect.top - 120, behavior: "smooth" });
        emphasize(entry.range); setTimeout(() => emphasize(null), 1200);
      }
    });
    entry.card = card;
    return card;
  }

  // ---- main ----------------------------------------------------------------
  const entries = [];                 // {comment, sec, range, start, end, outdated, card}
  const layers = new Map();           // sec.id -> layer element

  function addEntry(sec, map, comment) {
    const a = anchor(map, comment);
    const entry = { comment, sec, ...a };
    entries.push(entry);
    layers.get(sec.id).appendChild(renderCard(entry));
    return entry;
  }

  // selection → floating button → compose popover
  function installSelectionUI(sections) {
    const secSet = new Set(sections);
    const btn = el("button", { className: "mdb-selbtn", type: "button", hidden: true });
    btn.setAttribute("data-mdb", ""); btn.textContent = "💬 Comment";
    const pop = el("div", { className: "mdb-pop", hidden: true });
    pop.setAttribute("data-mdb", "");
    document.body.append(btn, pop);
    let pending = null;                // {sec, map, start, end, quote, prefix, suffix}

    function hideBtn() { btn.hidden = true; }
    function currentSelection() {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
      const r = sel.getRangeAt(0);
      if (!r.toString().trim()) return null;
      const startSec = (r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement)?.closest("section[id], header[id]");
      const endSec = (r.endContainer.nodeType === 1 ? r.endContainer : r.endContainer.parentElement)?.closest("section[id], header[id]");
      if (!startSec || startSec !== endSec || !secSet.has(startSec)) return null;
      // ignore selections inside our own UI
      for (let p = r.startContainer.parentElement; p; p = p.parentElement) if (p.hasAttribute && p.hasAttribute("data-mdb")) return null;
      return { r, sec: startSec };
    }
    function onSelect() {
      if (!pop.hidden) return;
      const s = currentSelection();
      if (!s) { hideBtn(); return; }
      const rect = s.r.getBoundingClientRect();
      btn.style.top = (window.scrollY + rect.top - 8) + "px";
      btn.style.left = (window.scrollX + rect.left + rect.width / 2) + "px";
      btn.hidden = false;
      const map = textMap(s.sec);
      const start = pointToOffset(map, s.r.startContainer, s.r.startOffset);
      const end = pointToOffset(map, s.r.endContainer, s.r.endOffset);
      pending = { sec: s.sec, map, start, end, quote: map.text.slice(start, end),
        prefix: map.text.slice(Math.max(0, start - CTX), start), suffix: map.text.slice(end, end + CTX) };
    }
    document.addEventListener("selectionchange", () => { clearTimeout(onSelect._t); onSelect._t = setTimeout(onSelect, 120); });
    document.addEventListener("scroll", () => { if (pop.hidden) onSelect(); }, true);

    btn.addEventListener("mousedown", (e) => e.preventDefault());   // keep the selection alive
    btn.addEventListener("click", () => { if (pending) openPop(); });

    function openPop() {
      pop.textContent = "";
      pop.appendChild(el("div", { className: "mdb-quote", textContent: norm(pending.quote) }));
      const name = el("input", { type: "text", placeholder: "Your name (optional)", value: localStorage.getItem(NAME_KEY) || "" });
      const body = el("textarea", { placeholder: "Comment on the highlighted text…" });
      const actions = el("div", { className: "mdb-actions" });
      const post = el("button", { type: "button", className: "mdb-post", textContent: "Comment" });
      const cancel = el("button", { type: "button", className: "mdb-cancel", textContent: "Cancel" });
      const msg = el("span", { className: "mdb-msg" });
      actions.append(post, cancel, msg);
      pop.append(name, body, actions);
      pop.style.top = btn.style.top; pop.style.left = btn.style.left;
      pop.hidden = false; btn.hidden = true; body.focus();

      const close = () => { pop.hidden = true; pending = null; window.getSelection()?.removeAllRanges(); };
      cancel.addEventListener("click", close);
      post.addEventListener("click", async () => {
        const text = body.value.trim();
        if (!text) { body.focus(); return; }
        const author = name.value.trim() || "Anonymous";
        localStorage.setItem(NAME_KEY, name.value.trim());
        post.disabled = true; msg.className = "mdb-msg"; msg.textContent = "Posting…";
        try {
          const saved = await api("POST", "/objects/comment", {
            spec: SLUG, section: pending.sec.id, author, body: text,
            quote: pending.quote, prefix: pending.prefix, suffix: pending.suffix,
            startOffset: pending.start, endOffset: pending.end, docVersion: hash(pending.map.text),
          });
          addEntry(pending.sec, textMap(pending.sec), saved);
          refreshHighlights(entries);
          close();
        } catch (e) { msg.className = "mdb-msg err"; msg.textContent = "Could not post — " + e.message; post.disabled = false; }
      });
    }
    // dismiss popover on outside click
    document.addEventListener("mousedown", (e) => { if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) { pop.hidden = true; pending = null; } });
  }

  // click highlighted text → flash its comment card
  function installHighlightClick() {
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest("[data-mdb]")) return;   // ignore clicks on our own UI
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;            // a drag-select, not a click
      let node, off;
      if (document.caretPositionFromPoint) { const cp = document.caretPositionFromPoint(e.clientX, e.clientY); if (!cp) return; node = cp.offsetNode; off = cp.offset; }
      else if (document.caretRangeFromPoint) { const r = document.caretRangeFromPoint(e.clientX, e.clientY); if (!r) return; node = r.startContainer; off = r.startOffset; }
      else return;
      const sec = (node.nodeType === 1 ? node : node.parentElement)?.closest("section[id], header[id]");
      if (!sec || !layers.has(sec.id)) return;
      const map = textMap(sec);
      const at = pointToOffset(map, node, off);
      const hit = entries.find((en) => en.sec === sec && !en.outdated && at >= en.start && at < en.end);
      if (hit && hit.card) { hit.card.scrollIntoView({ block: "center", behavior: "smooth" }); emphasize(hit.range); setTimeout(() => emphasize(null), 1200); }
    });
  }

  async function boot() {
    injectCSS();
    const sections = [...document.querySelectorAll(SECTION_SEL)].filter((s) => s.id);
    if (!sections.length) return;
    for (const sec of sections) { sec.classList.add("mdb-sec"); const layer = el("div", { className: "mdb-layer" }); layer.setAttribute("data-mdb", ""); sec.appendChild(layer); layers.set(sec.id, layer); }

    let bySection = {};
    try {
      await ensureBackend();
      const data = await api("GET", `/objects/comment?spec=${encodeURIComponent(SLUG)}&sort=_created_at&limit=1000`);
      for (const c of (data?.objects || [])) (bySection[c.section] ||= []).push(c);
    } catch (e) { console.warn("[spec-comments] backend error:", e.message); }

    for (const sec of sections) {
      const list = bySection[sec.id] || [];
      if (!list.length) continue;
      const map = textMap(sec);
      for (const c of list) addEntry(sec, map, c);
    }
    refreshHighlights(entries);
    installSelectionUI(sections);
    installHighlightClick();
    // re-anchor highlights after late layout shifts (fonts) — ranges stay valid; just recompute is unneeded for Highlight API
    window.addEventListener("resize", () => refreshHighlights(entries));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
