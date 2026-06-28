/* ---------------------------------------------------------------------------
 * MorphDB spec comments — Notion-style inline margin comments.
 *
 * Each spec page sets `window.SPEC_COMMENTS = { slug: "permissions" }` and loads
 * this one script. For every commentable section it anchors a comment thread in
 * the RIGHT MARGIN (gutter), Notion-style:
 *   • no comments  → a discreet "+" button that fades in when you hover the section
 *   • has comments → the full comment(s) shown in the gutter, always visible,
 *                     with a small "Comment" button to add another
 * On narrow screens the gutter collapses and the thread flows under the heading.
 *
 * Comments live in the CLOUD-hosted MorphDB under the app `morphdb-spec-comments`.
 * NOTE: this deliberately hardcodes the hosted endpoint (no localhost branch, no
 * env vars) so every visitor to the public docs shares the same threads.
 * ------------------------------------------------------------------------- */
(() => {
  const CFG = window.SPEC_COMMENTS || {};
  const SLUG = CFG.slug || "spec";
  const HOST = CFG.host || "https://hjvvxxmlcrp6jzkfaohctk7cda0qshum.lambda-url.us-west-1.on.aws";
  const APP  = CFG.app  || "morphdb-spec-comments";
  const SECTION_SEL = CFG.sectionSelector || "main > section[id], main > header[id]";
  const NAME_KEY = "morphdb-spec:name";

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

  // Self-bootstrap so a fork works against a fresh backend.
  async function ensureBackend() {
    let exists = false;
    try {
      const r = await fetch(HOST + "/schema/comment", { headers: { "X-App-Key": APP } });
      exists = r.ok;
    } catch (_) {}
    if (!exists) {
      await fetch(HOST + "/app", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: APP }),
      }).catch(() => {});
    }
    await api("PUT", "/schema/comment", {
      merge: true,
      fields: {
        spec:    { type: "string", index: true },
        section: { type: "string", index: true },
        author:  { type: "string" },
        body:    { type: "string" },
      },
    });
  }

  function ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 2592000) return Math.floor(s / 86400) + "d ago";
    return new Date(iso).toLocaleDateString();
  }

  function injectCSS() {
    const css = `
    .mdb-sec{position:relative}
    .mdb-anchor{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:10px 0 4px}
    .mdb-cards{display:flex;flex-direction:column;gap:8px}
    .mdb-card{background:var(--cyan-bg,rgba(115,184,208,.07));border:1px solid var(--line,#27424f);
      border-left:2px solid var(--cyan,#73b8d0);border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.45}
    .mdb-card-meta{font-size:11px;color:var(--ink-3,#8aa0ad);margin-bottom:3px}
    .mdb-card-meta b{color:var(--ink,#ECE4D2);font-weight:600}
    .mdb-card-meta time{margin-left:6px}
    .mdb-card-body{color:var(--ink-2,#b9c6cf);white-space:pre-wrap;overflow-wrap:anywhere}
    .mdb-add{display:inline-flex;align-items:center;gap:6px;cursor:pointer;font:inherit;font-size:12.5px;
      background:transparent;color:var(--ink-3,#8aa0ad);border:1px solid transparent;border-radius:7px;padding:3px 8px;
      transition:opacity .12s,color .12s,background .12s,border-color .12s}
    .mdb-add .mdb-plus{font-size:15px;line-height:1}
    .mdb-add:hover{color:var(--brass,#E7B24C);background:var(--cyan-bg,rgba(115,184,208,.06));border-color:var(--line,#27424f)}
    .mdb-reply{margin-top:2px;cursor:pointer;font:inherit;font-size:12px;color:var(--cyan,#73b8d0);
      background:transparent;border:none;padding:2px 0;text-align:left;align-self:flex-start}
    .mdb-reply:hover{text-decoration:underline}
    .mdb-compose{display:flex;flex-direction:column;gap:6px;margin-top:4px}
    .mdb-compose input,.mdb-compose textarea{font:inherit;font-size:13px;color:var(--ink,#ECE4D2);
      background:rgba(0,0,0,.25);border:1px solid var(--line,#27424f);border-radius:6px;padding:6px 8px;width:100%}
    .mdb-compose textarea{min-height:54px;resize:vertical}
    .mdb-compose input::placeholder,.mdb-compose textarea::placeholder{color:var(--ink-3,#8aa0ad)}
    .mdb-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .mdb-actions button{font:inherit;font-size:12px;font-weight:600;cursor:pointer;border-radius:6px;padding:5px 12px}
    .mdb-post{background:var(--brass,#E7B24C);color:#1a1206;border:none}
    .mdb-post:disabled{opacity:.5;cursor:default}
    .mdb-cancel{background:transparent;color:var(--ink-3,#8aa0ad);border:1px solid var(--line,#27424f)}
    .mdb-msg{font-size:11px;color:var(--ink-3,#8aa0ad)}
    .mdb-msg.err{color:var(--deny,#e0664b)}
    /* WIDE: real right-margin gutter, Notion-style */
    @media (min-width:1200px){
      main{padding-right:248px}
      .mdb-anchor{position:absolute;left:100%;margin:0 0 0 18px;top:2px;width:212px}
      .mdb-add-label{display:none}
      .mdb-anchor[data-has="0"] .mdb-add{opacity:0;pointer-events:none;border:1px dashed var(--line,#27424f);
        border-radius:50%;width:26px;height:26px;justify-content:center;padding:0}
      .mdb-anchor[data-has="0"] .mdb-add .mdb-plus{font-size:16px}
      .mdb-sec:hover .mdb-anchor[data-has="0"] .mdb-add,
      .mdb-anchor:focus-within .mdb-add{opacity:1;pointer-events:auto}
    }
    /* NARROW: collapse the gutter, flow under the heading */
    @media (max-width:1199px){
      .mdb-anchor{position:static;width:auto;max-width:70ch}
      .mdb-add{opacity:.8}
    }`;
    const s = document.createElement("style");
    s.textContent = css;
    document.head.appendChild(s);
  }

  // Build a comment card with NO innerHTML — user text via textContent only, so a
  // comment body of `<img onerror=…>` is shown literally, never parsed/executed.
  function renderCard(c) {
    const card = document.createElement("div");
    card.className = "mdb-card";
    const meta = document.createElement("div");
    meta.className = "mdb-card-meta";
    const who = document.createElement("b");
    who.textContent = c.author || "Anonymous";
    meta.appendChild(who);
    if (c._created_at) {
      const t = document.createElement("time");
      t.dateTime = c._created_at;
      t.textContent = "· " + ago(c._created_at);
      meta.appendChild(t);
    }
    const body = document.createElement("div");
    body.className = "mdb-card-body";
    body.textContent = c.body || "";
    card.append(meta, body);
    return card;
  }

  function el(tag, props) { return Object.assign(document.createElement(tag), props); }

  function mountSection(sec, initial) {
    const heading = sec.querySelector("h1, h2");
    if (!heading) return;
    sec.classList.add("mdb-sec");
    const id = sec.id;
    const title = heading.textContent.trim();
    const after = heading.closest(".shead") || heading;

    const anchor = el("div", { className: "mdb-anchor" });
    const cards = el("div", { className: "mdb-cards" });
    const add = el("button", { type: "button", className: "mdb-add" });
    add.setAttribute("aria-label", `Comment on “${title}”`);
    add.append(el("span", { className: "mdb-plus", textContent: "+" }),
               el("span", { className: "mdb-add-label", textContent: "Comment" }));
    const reply = el("button", { type: "button", className: "mdb-reply", textContent: "Comment" });
    const form = el("form", { className: "mdb-compose", hidden: true });
    const nameI = el("input", { type: "text", placeholder: "Your name (optional)", value: localStorage.getItem(NAME_KEY) || "" });
    const bodyI = el("textarea", { placeholder: `Comment on “${title}”…` });
    const actions = el("div", { className: "mdb-actions" });
    const post = el("button", { type: "submit", className: "mdb-post", textContent: "Comment" });
    const cancel = el("button", { type: "button", className: "mdb-cancel", textContent: "Cancel" });
    const msg = el("span", { className: "mdb-msg" });
    actions.append(post, cancel, msg);
    form.append(nameI, bodyI, actions);
    anchor.append(cards, reply, add, form);
    after.insertAdjacentElement("afterend", anchor);

    const items = initial.slice();
    let composing = false;

    function render() {
      cards.textContent = "";
      items.forEach((c) => cards.appendChild(renderCard(c)));
      const has = items.length > 0;
      anchor.dataset.has = has ? "1" : "0";
      add.hidden = has || composing;
      reply.hidden = !has || composing;
      form.hidden = !composing;
    }
    function open() { composing = true; render(); bodyI.focus(); }
    function close() { composing = false; msg.textContent = ""; msg.className = "mdb-msg"; render(); }

    add.addEventListener("click", open);
    reply.addEventListener("click", open);
    cancel.addEventListener("click", close);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = bodyI.value.trim();
      if (!body) { bodyI.focus(); return; }
      const author = nameI.value.trim() || "Anonymous";
      localStorage.setItem(NAME_KEY, nameI.value.trim());
      post.disabled = true; msg.className = "mdb-msg"; msg.textContent = "Posting…";
      try {
        const saved = await api("POST", "/objects/comment", { spec: SLUG, section: id, author, body });
        items.push(saved);
        bodyI.value = "";
        composing = false;
        render();
      } catch (e) {
        msg.className = "mdb-msg err";
        msg.textContent = "Could not post — " + e.message;
      } finally {
        post.disabled = false;
      }
    });

    render();
  }

  async function boot() {
    injectCSS();
    const sections = [...document.querySelectorAll(SECTION_SEL)].filter((s) => s.id);
    if (!sections.length) return;
    let bySection = {};
    try {
      await ensureBackend();
      const data = await api("GET", `/objects/comment?spec=${encodeURIComponent(SLUG)}&sort=_created_at&limit=1000`);
      for (const c of (data?.objects || [])) (bySection[c.section] ||= []).push(c);
    } catch (e) {
      console.warn("[spec-comments] backend error:", e.message);
    }
    for (const sec of sections) mountSection(sec, bySection[sec.id] || []);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
