/* M3 Operator Console — guided work queue (no phase architecture in UI) */
(function () {
  "use strict";

  const BUILD = "20260929-m3-owner-ui-15-minute-operator-training";
  const main = document.getElementById("main");
  const toastEl = document.getElementById("toast");
  const hintBar = document.getElementById("hint-bar");
  const trainingToggle = document.getElementById("training-mode");
  const walkthroughDlg = document.getElementById("walkthrough-dialog");

  let route = "home";
  let dirty = false;
  let callState = null;
  let training = localStorage.getItem("m3_training_mode") === "1";
  let walkthroughDone = localStorage.getItem("m3_walkthrough_done") === "1";

  trainingToggle.checked = training;

  function toast(msg) {
    toastEl.hidden = false;
    toastEl.textContent = msg;
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(() => { toastEl.hidden = true; }, 2800);
  }

  function setHint(text) {
    if (!training || !text) {
      hintBar.hidden = true;
      hintBar.textContent = "";
      return;
    }
    hintBar.hidden = false;
    hintBar.textContent = text;
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      ...opts,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.message || data.detail)) || "Something went wrong. Your notes were kept if already saved.";
      const err = new Error(typeof msg === "string" ? msg : "Request failed");
      err.data = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function confirmLeave() {
    if (!dirty) return true;
    return window.confirm("You have unsaved changes. Leave anyway?");
  }

  function navigate(next, params) {
    if (!confirmLeave()) return;
    dirty = false;
    callState = null;
    route = next;
    const hash = params ? `#/${next}?${new URLSearchParams(params)}` : `#/${next}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
    document.querySelectorAll(".nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.route === next || (next.startsWith("deal") && b.dataset.route === "deals") || (next === "call" && b.dataset.route === "calls"));
    });
    render();
  }

  function parseHash() {
    const raw = (location.hash || "#/home").replace(/^#\/?/, "");
    const [path, qs] = raw.split("?");
    const params = Object.fromEntries(new URLSearchParams(qs || ""));
    return { path: path || "home", params };
  }

  function badge(status, color) {
    return `<span class="badge ${esc(color || "gray")}" aria-label="Status ${esc(status)}">${esc(status)}</span>`;
  }

  function priorityBadge(p) {
    if (!p) return "";
    const c = p === "HIGH" ? "red" : p === "LOW" ? "gray" : "blue";
    return `<span class="badge ${c}">${esc(p)}</span>`;
  }

  function urgencyBadge(u) {
    if (!u || !u.label) return "";
    const crit = u.level === "critical" || u.level === "high";
    return `<span class="badge ${crit ? "urgent" : "yellow"}">${esc(u.label)}</span>`;
  }

  function dealCard(d, opts) {
    opts = opts || {};
    const title = `${d.buyer || "Buyer"} — ${d.product || "Product"}`;
    const primary = opts.primaryLabel || d.ui_next_action_label || "OPEN DEAL";
    const action = opts.action || "open";
    return `
      <article class="deal-card" data-deal-id="${esc(d.deal_id)}">
        <div class="deal-card-top">
          <h3 class="deal-title">${esc(title)}</h3>
          ${badge(d.ui_status, d.ui_status_color)}
          ${priorityBadge(d.ui_priority)}
          ${urgencyBadge(d.deadline_urgency)}
          ${d.call_priority ? `<span class="badge green">${esc(d.call_priority)}</span>` : ""}
        </div>
        <div class="meta-row">
          ${d.solicitation ? `<span>Solicitation: ${esc(d.solicitation)}</span>` : ""}
          ${d.deadline ? `<span>Deadline: ${esc(d.deadline)}</span>` : ""}
          ${d.supplier ? `<span>Supplier: ${esc(d.supplier)}</span>` : ""}
          ${d.phone ? `<span>Phone: ${esc(d.phone)}</span>` : ""}
        </div>
        <p class="why"><strong>Why:</strong> ${esc(d.why || d.ui_next_action_reason || "—")}</p>
        ${d.operator_note || d.plain ? `<p class="muted">${esc(d.operator_note || d.plain)}</p>` : ""}
        <div class="deal-actions">
          <button type="button" class="btn primary" data-act="${esc(action)}" data-id="${esc(d.deal_id)}">${esc(primary)}</button>
          ${opts.secondary || ""}
        </div>
      </article>`;
  }

  function emptyBox(msg) {
    return `<div class="empty">${esc(msg || "Nothing here right now.")}</div>`;
  }

  async function renderHome() {
    setHint("Start here.");
    const data = await api("/api/ui/home");
    const cards = (data.cards || []).map((c) => `
      <button type="button" class="stat-card ${esc(c.color)}" data-go="${esc(c.id)}">
        <span class="label">${esc(c.label)}</span>
        <span class="count">${esc(c.count)}</span>
      </button>`).join("");
    const s = data.summary || {};
    main.innerHTML = `
      <h1>Home</h1>
      <p class="lead">${esc(data.headline || "What needs my attention today?")}</p>
      ${data.caught_up ? `<div class="caught-up"><h2>You're caught up.</h2><p>${esc(data.next_useful || "")}</p></div>` : ""}
      <div class="home-grid">${cards}</div>
      <div class="summary-strip" aria-label="Business summary">
        <span>Active deals <strong>${esc(s.active_deals)}</strong></span>
        <span>Calls due <strong>${esc(s.calls_due)}</strong></span>
        <span>Quotes pending <strong>${esc(s.quotes_pending)}</strong></span>
        <span>Bid-ready <strong>${esc(s.bid_ready)}</strong></span>
        <span class="muted">${esc(s.note || "")}</span>
      </div>
      <p style="margin-top:1rem"><button type="button" class="btn primary" data-route="today">Open Today's Work</button></p>
    `;
    main.querySelectorAll("[data-go]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-go");
        if (id === "quotes") navigate("quotes");
        else if (id === "registrations") navigate("registrations");
        else if (id === "bid_prep") navigate("bid-prep");
        else if (id === "blocked") navigate("blocked");
        else navigate("today", { section: id });
      });
    });
    main.querySelector("[data-route=today]")?.addEventListener("click", () => navigate("today"));
  }

  async function renderToday(params) {
    setHint("Work the top CALL TODAY item first.");
    const data = await api("/api/ui/today");
    const focus = params.section || "call_today";
    const order = ["call_today", "follow_up", "quotes", "registrations", "owner_actions", "responses", "bid_prep", "submissions", "awaiting_result", "blocked"];
    let html = `<h1>Today's Work</h1><p class="lead">Choose the top item → do the next action → save → move on.</p>`;
    if (data.caught_up) {
      html += `<div class="caught-up"><h2>You're caught up.</h2><p>Review the Watch list or check registrations for future unlocks.</p></div>`;
    }
    for (const key of order) {
      const sec = (data.sections || {})[key];
      if (!sec) continue;
      const highlight = key === focus ? " style=\"scroll-margin-top:120px\"" : "";
      html += `<section class="section" id="sec-${key}"${highlight}>
        <div class="section-head"><h2>${esc(sec.title)}</h2><span class="muted">${(sec.items || []).length}${(sec.total_available != null) ? " / " + sec.total_available : ""}</span></div>
        ${(sec.items || []).length ? sec.items.map((d) => dealCard(d, {
          primaryLabel: d.ui_next_action_label || "OPEN DEAL",
          action: d.ui_next_action === "CALL_SUPPLIER" ? "call" : (d.ui_next_action === "REGISTER" ? "register" : "open"),
        })).join("") : emptyBox(sec.empty)}
      </section>`;
    }
    main.innerHTML = html;
    bindDealActions();
    if (focus) document.getElementById(`sec-${focus}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function bindDealActions() {
    main.querySelectorAll("[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-id");
        const act = btn.getAttribute("data-act");
        if (act === "call") navigate("call", { id });
        else if (act === "register") {
          const portal = id.startsWith("reg:") ? id.slice(4) : id;
          navigate("registration", { id: portal });
        } else if (act === "quote") navigate("quote", { id });
        else navigate("deal", { id });
      });
    });
  }

  async function renderDeals(params) {
    setHint("Filter only when you need to find a specific deal.");
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.status) qs.set("status", params.status);
    if (params.page) qs.set("page", params.page);
    qs.set("page_size", "40");
    const data = await api("/api/ui/deals?" + qs.toString());
    main.innerHTML = `
      <h1>Deals</h1>
      <div class="filters">
        <input id="f-q" type="search" placeholder="Search…" value="${esc(params.q || "")}">
        <select id="f-status">
          <option value="">All statuses</option>
          ${["CALL SUPPLIER","FOLLOW UP","WAITING FOR QUOTE","QUOTE RECEIVED","REGISTER FIRST","BID PREP","WATCH","BLOCKED","SKIP"].map((s) =>
            `<option value="${esc(s)}" ${params.status === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select>
        <button type="button" class="btn secondary" id="f-go">Apply</button>
      </div>
      ${(data.items || []).length ? data.items.map((d) => dealCard(d)).join("") : emptyBox("No deals match these filters.")}
      <div class="pager">
        <button type="button" class="btn ghost" id="prev" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted">Page ${esc(data.page)} · ${esc(data.total)} total</span>
        <button type="button" class="btn ghost" id="next" ${data.has_more ? "" : "disabled"}>Next</button>
      </div>`;
    bindDealActions();
    document.getElementById("f-go").onclick = () => navigate("deals", {
      q: document.getElementById("f-q").value,
      status: document.getElementById("f-status").value,
      page: "1",
    });
    document.getElementById("prev").onclick = () => navigate("deals", { ...params, page: String(Math.max(1, (data.page || 1) - 1)) });
    document.getElementById("next").onclick = () => navigate("deals", { ...params, page: String((data.page || 1) + 1) });
  }

  async function renderDeal(params) {
    setHint("One primary action — use it.");
    const data = await api("/api/ui/deals/" + encodeURIComponent(params.id));
    const w = data.what || {};
    const next = data.next || {};
    const suppliers = (data.suppliers || []).map((s, i) => `
      <li><strong>${esc(s.call_priority || "")}</strong> ${esc(s.company || s.supplier_id)}
        ${s.phone ? ` · ${esc(s.phone)}` : ""}
        ${s.rfq_url ? ` · <a href="${esc(s.rfq_url)}" target="_blank" rel="noopener">RFQ</a>` : ""}
        <div class="muted">${esc(s.why || "")}</div>
      </li>`).join("");
    main.innerHTML = `
      <p><button type="button" class="btn ghost" data-back>← Back</button></p>
      <h1>${esc(w.buyer)} — ${esc(w.product)}</h1>
      <div class="deal-card-top" style="margin-bottom:1rem">
        ${badge(data.ui_status, data.ui_status_color)}
        ${priorityBadge(data.ui_priority)}
        ${urgencyBadge(w.deadline_urgency)}
      </div>
      <div class="layout-deal">
        <div>
          <section class="panel">
            <h2>What is this?</h2>
            <div class="meta-row">
              <span>Buyer: ${esc(w.buyer)}</span>
              <span>Solicitation: ${esc(w.solicitation || "—")}</span>
              <span>Product: ${esc(w.product)}</span>
              <span>Qty: ${esc(w.quantity ?? "—")}</span>
              <span>Deadline: ${esc(w.deadline || "—")}</span>
              <span>Delivery: ${esc(w.delivery_requirement || "—")}</span>
            </div>
          </section>
          <section class="panel" style="margin-top:.75rem">
            <h2>Why do we care?</h2>
            <p>${esc((data.why_we_care || {}).summary || "")}</p>
            <p class="muted">Evidence: ${esc((data.why_we_care || {}).evidence_quality || "—")}
              · Supplier path: ${(data.why_we_care || {}).strong_supplier_path ? "Yes" : "Building"}</p>
          </section>
          <section class="panel" style="margin-top:.75rem">
            <h2>Suppliers</h2>
            ${suppliers ? `<ol>${suppliers}</ol>` : emptyBox("No suppliers listed yet.")}
          </section>
        </div>
        <aside class="panel">
          <h2>What do I do next?</h2>
          <p class="why">${esc(next.reason || "")}</p>
          <button type="button" class="btn primary block" id="primary-next">${esc(next.label || "OPEN")}</button>
          <button type="button" class="btn ghost block" id="secondary-open-call" style="margin-top:.5rem">Open call workspace</button>
          ${data.blocked ? `<div class="still-needed" style="margin-top:1rem"><strong>${esc(data.blocked.blocker)}</strong><p>${esc(data.blocked.plain)}</p><p class="muted">${esc(data.blocked.resolves_with)}</p></div>` : ""}
          <details class="advanced" id="adv">
            <summary>Show Details</summary>
            <p class="muted">Loading only when opened…</p>
          </details>
        </aside>
      </div>`;
    main.querySelector("[data-back]").onclick = () => navigate("today");
    document.getElementById("primary-next").onclick = () => {
      const a = next.action;
      if (a === "CALL_SUPPLIER" || a === "FOLLOW_UP") navigate("call", { id: data.deal_id });
      else if (a === "REVIEW_QUOTE") navigate("quote", { id: data.deal_id });
      else if (a === "REGISTER") navigate("registrations");
      else if (a === "START_BID_PREP") navigate("bid-prep");
      else if (a === "RESOLVE_BLOCKER") navigate("blocked");
      else navigate("today");
    };
    document.getElementById("secondary-open-call").onclick = () => navigate("call", { id: data.deal_id });
    const adv = document.getElementById("adv");
    adv.addEventListener("toggle", async () => {
      if (!adv.open || adv.dataset.loaded) return;
      try {
        const a = await api("/api/ui/advanced/" + encodeURIComponent(data.deal_id));
        adv.dataset.loaded = "1";
        adv.innerHTML = `<summary>Show Details</summary>
          <p class="muted">Owner / advanced only — not required for daily work.</p>
          <pre>${esc(JSON.stringify(a, null, 2))}</pre>`;
      } catch (e) {
        adv.innerHTML = `<summary>Show Details</summary><p>${esc(e.message)}</p>`;
      }
    });
  }

  function answerInput(q) {
    const id = esc(q.question_id);
    const val = q.answer_value != null ? esc(q.answer_value) : "";
    const type = q.answer_type || "text";
    let control = "";
    if (type === "yes_no" || type === "yes_no_unknown") {
      const opts = type === "yes_no_unknown" ? ["YES", "NO", "UNKNOWN"] : ["YES", "NO"];
      control = `<select data-qid="${id}" data-field="answer">${opts.map((o) =>
        `<option value="${o}" ${String(q.answer_value).toUpperCase() === o ? "selected" : ""}>${o}</option>`).join("")}<option value="" ${q.answer_value == null || q.answer_value === "" ? "selected" : ""}>—</option></select>`;
    } else if (type === "dropdown" && Array.isArray(q.options)) {
      control = `<select data-qid="${id}" data-field="answer"><option value="">—</option>${q.options.map((o) =>
        `<option value="${esc(o)}" ${String(q.answer_value) === String(o) ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    } else if (type === "currency" || type === "number") {
      control = `<input data-qid="${id}" data-field="answer" type="number" step="0.01" value="${val}" inputmode="decimal">`;
    } else if (type === "date") {
      control = `<input data-qid="${id}" data-field="answer" type="date" value="${val}">`;
    } else if (type === "multiline") {
      control = `<textarea data-qid="${id}" data-field="answer">${val}</textarea>`;
    } else {
      control = `<input data-qid="${id}" data-field="answer" type="text" value="${val}">`;
    }
    return `
      <div class="q-item ${q.priority_band === "MUST_ASK" ? "must" : "relevant"}">
        <div class="field">
          <label for="q-${id}">${esc(q.question_text || q.question_id)}</label>
          ${control}
          <label class="hint" for="n-${id}">Note</label>
          <input id="n-${id}" data-qid="${id}" data-field="note" type="text" value="${esc(q.owner_note || "")}" placeholder="Optional note for this answer">
        </div>
      </div>`;
  }

  async function renderCall(params) {
    setHint("Call this supplier. Enter the price they give you. Save when finished.");
    const qs = new URLSearchParams({ deal_id: params.id });
    if (params.supplier_id) qs.set("supplier_id", params.supplier_id);
    const data = await api("/api/ui/calls/workspace?" + qs.toString());
    callState = data;
    const must = ((data.what_to_ask || {}).MUST_ASK || []).map(answerInput).join("");
    const rel = ((data.what_to_ask || {}).ASK_IF_RELEVANT || []).map(answerInput).join("");
    const s = data.supplier || {};
    const p = data.product || {};
    main.innerHTML = `
      <p><button type="button" class="btn ghost" data-back>← Back</button></p>
      <h1>Call workspace</h1>
      <div class="layout-deal layout-call">
        <div>
          <section class="panel">
            <h2>Supplier</h2>
            <p><strong>${esc(s.company)}</strong> · <span class="badge green">${esc(s.call_priority || "CALL FIRST")}</span></p>
            <div class="meta-row">
              <span>Phone: ${s.phone ? `<a href="tel:${esc(s.phone)}">${esc(s.phone)}</a>` : "—"}</span>
              <span>Contact: ${esc(s.contact || "—")}</span>
              <span>${s.rfq_url ? `<a href="${esc(s.rfq_url)}" target="_blank" rel="noopener">RFQ form</a>` : "No RFQ link"}</span>
            </div>
            <p class="why"><strong>Why this supplier:</strong> ${esc(s.why || "")}</p>
          </section>
          <section class="panel" style="margin-top:.75rem">
            <h2>Product</h2>
            <div class="meta-row">
              <span>${esc(p.exact_item)}</span>
              <span>Qty: ${esc(p.quantity ?? "—")}</span>
              <span>Delivery: ${esc(p.required_delivery || "—")}</span>
              <span>Deadline: ${esc(p.deadline || "—")}</span>
            </div>
          </section>
          ${(data.other_suppliers || []).length ? `<section class="panel" style="margin-top:.75rem"><h2>Other suppliers</h2>
            <ul>${data.other_suppliers.map((o) => `<li><button type="button" class="btn ghost" data-switch="${esc(o.supplier_id)}">${esc(o.call_priority)} — ${esc(o.company)}</button></li>`).join("")}</ul>
          </section>` : ""}
        </div>
        <div>
          <section class="panel">
            <h2>Must ask</h2>
            <div class="q-list">${must || emptyBox("No must-ask questions.")}</div>
            <h2 style="margin-top:1rem">Ask if relevant</h2>
            <div class="q-list">${rel || emptyBox("None for this product.")}</div>
          </section>
          <section class="panel" style="margin-top:.75rem">
            <div class="field">
              <label for="call-notes">Notes</label>
              <textarea id="call-notes">${esc(data.call_notes || "")}</textarea>
            </div>
            <div class="field">
              <label for="promised">Quote promised by</label>
              <input id="promised" type="date" value="${esc((data.follow_up || {}).promised_quote_date || "")}">
              <div class="hint">If they promise a quote, set the date — deal moves to Follow Up.</div>
            </div>
            <div id="still-needed" class="still-needed" hidden></div>
            <div class="deal-actions">
              <button type="button" class="btn primary" id="save-call">Save Call</button>
              <button type="button" class="btn secondary" id="complete-call">Mark Complete</button>
            </div>
            <p class="muted" id="save-feedback"></p>
          </section>
        </div>
      </div>`;
    dirty = false;
    main.querySelectorAll("[data-qid], #call-notes, #promised").forEach((el) => {
      el.addEventListener("input", () => { dirty = true; });
      el.addEventListener("change", () => { dirty = true; });
    });
    main.querySelector("[data-back]").onclick = () => navigate("today");
    main.querySelectorAll("[data-switch]").forEach((b) => {
      b.onclick = () => navigate("call", { id: params.id, supplier_id: b.getAttribute("data-switch") });
    });
    document.getElementById("save-call").onclick = () => saveCall(false);
    document.getElementById("complete-call").onclick = () => saveCall(true);
  }

  function collectAnswers() {
    const byQ = {};
    main.querySelectorAll("[data-qid][data-field=answer]").forEach((el) => {
      const qid = el.getAttribute("data-qid");
      byQ[qid] = byQ[qid] || { question_id: qid };
      let v = el.value;
      if (el.type === "number" && v !== "") v = Number(v);
      byQ[qid].answer_value = v === "" ? null : v;
    });
    main.querySelectorAll("[data-qid][data-field=note]").forEach((el) => {
      const qid = el.getAttribute("data-qid");
      byQ[qid] = byQ[qid] || { question_id: qid };
      byQ[qid].owner_note = el.value || null;
    });
    return Object.values(byQ).filter((a) => a.answer_value != null || a.owner_note);
  }

  async function saveCall(complete) {
    if (!callState) return;
    const body = {
      session_id: callState.session_id,
      answers: collectAnswers(),
      call_notes: document.getElementById("call-notes").value,
      promised_quote_date: document.getElementById("promised").value || null,
      complete: !!complete,
      outcome: document.getElementById("promised").value ? "QUOTE_PROMISED" : null,
    };
    try {
      const res = await api("/api/ui/calls/save?deal_id=" + encodeURIComponent(callState.deal_id), {
        method: "POST",
        body: JSON.stringify(body),
      });
      dirty = false;
      const fb = document.getElementById("save-feedback");
      const when = res.saved_at ? new Date(res.saved_at).toLocaleString() : new Date().toLocaleString();
      if (fb) fb.textContent = `${res.message || "Saved"} · ${when}`;
      toast(`${res.message || "Saved"} · ${when}`);
      const still = document.getElementById("still-needed");
      if (res.still_needed && res.still_needed.length) {
        still.hidden = false;
        still.innerHTML = `<strong>Still needed</strong><ul>${res.still_needed.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
      } else if (still) {
        still.hidden = true;
      }
      if (complete && res.ok) {
        setTimeout(() => navigate("today"), 600);
      }
      if (res.follow_up_queued) toast("Saved · moved toward Follow Up");
    } catch (e) {
      toast(e.message || "Save failed — notes preserved if previously saved.");
      const retry = window.confirm((e.message || "Save failed") + "\n\nRetry?");
      if (retry) saveCall(complete);
    }
  }

  async function renderCalls() {
    setHint("Highest-priority supplier calls.");
    const data = await api("/api/ui/calls");
    main.innerHTML = `
      <h1>Calls</h1>
      <p class="lead">Work these like a queue — top first.</p>
      <h2>Call today</h2>
      ${(data.items || []).length ? data.items.map((d) => dealCard(d, { action: "call", primaryLabel: "CALL SUPPLIER" })).join("") : emptyBox(data.empty)}
      <h2 style="margin-top:1.25rem">Follow up</h2>
      ${(data.follow_up || []).length ? data.follow_up.map((d) => dealCard(d, { action: "call", primaryLabel: "FOLLOW UP" })).join("") : emptyBox("No follow-ups due.")}
    `;
    bindDealActions();
  }

  async function renderQuotes() {
    setHint("Review new quotes before bidding.");
    const data = await api("/api/ui/quotes");
    let html = `<h1>Quotes</h1><p class="lead">Simple quote queue.</p>`;
    for (const sec of data.sections || []) {
      html += `<section class="section"><div class="section-head"><h2>${esc(sec.title)}</h2></div>`;
      if (!(sec.items || []).length) html += emptyBox(sec.empty);
      else {
        html += sec.items.map((q) => `
          <article class="deal-card">
            <div class="deal-card-top">
              <h3 class="deal-title">${esc(q.buyer || "Buyer")} — ${esc(q.product || "Quote")}</h3>
              ${badge(sec.title, "yellow")}
            </div>
            <div class="meta-row">
              ${q.unit_price != null ? `<span>Unit: ${esc(q.unit_price)}</span>` : ""}
              ${q.recommendation ? `<span>${esc(q.recommendation)}</span>` : ""}
            </div>
            <button type="button" class="btn primary" data-act="quote" data-id="${esc(q.deal_id)}">REVIEW QUOTE</button>
          </article>`).join("");
      }
      html += `</section>`;
    }
    main.innerHTML = html;
    bindDealActions();
  }

  async function renderQuote(params) {
    const data = await api("/api/ui/quotes/" + encodeURIComponent(params.id));
    const sq = data.supplier_quote || {};
    const ec = data.deal_economics || {};
    const rows = ((data.comparison || {}).rows || []).map((r) => `
      <tr>
        <td>${esc(r.supplier)}</td><td>${esc(r.landed_cost ?? "—")}</td><td>${esc(r.delivery ?? "—")}</td>
        <td>${esc(r.terms ?? "—")}</td><td>${esc(r.expected_profit ?? "—")}</td><td>${esc(r.recommendation ?? "—")}</td>
      </tr>`).join("");
    main.innerHTML = `
      <p><button type="button" class="btn ghost" data-back>← Back</button></p>
      <h1>Quote review</h1>
      <div class="layout-deal">
        <section class="panel">
          <h2>Supplier quote</h2>
          <div class="field"><label>Unit price</label><input id="uq-unit" type="number" step="0.01" value="${esc(sq.unit_price ?? "")}"></div>
          <div class="field"><label>Freight</label><input id="uq-freight" type="number" step="0.01" value="${esc(sq.freight ?? "")}"></div>
          <div class="field"><label>Lead time</label><input id="uq-lead" type="text" value="${esc(sq.lead_time ?? "")}"></div>
          <div class="field"><label>Terms</label><input id="uq-terms" type="text" value="${esc(sq.terms ?? "")}"></div>
          <div class="field"><label>Quote expiration</label><input id="uq-exp" type="date" value="${esc(sq.quote_expiration ?? "")}"></div>
          <p>Total landed: <strong>${esc(sq.total_landed ?? "—")}</strong></p>
        </section>
        <section class="panel">
          <h2>Deal economics <span class="badge gray">Owner only</span></h2>
          <p class="muted">${esc(ec.note || "")}</p>
          <div class="meta-row">
            <span>Expected revenue: ${esc(ec.expected_revenue ?? "—")}</span>
            <span>Landed cost: ${esc(ec.landed_cost ?? "—")}</span>
            <span>Expected profit: ${esc(ec.expected_profit ?? "—")}</span>
            <span>Margin: ${esc(ec.margin ?? "—")}</span>
            <span>Max-buy: ${esc(ec.max_buy ?? "—")}</span>
          </div>
          <div class="field"><label>Recommendation</label>
            <select id="uq-rec">${(data.recommendation_options || []).map((o) =>
              `<option value="${esc(o)}" ${data.recommendation === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>
          </div>
          <button type="button" class="btn primary" id="save-quote">Save</button>
          <p class="muted" id="save-feedback"></p>
        </section>
      </div>
      <section class="panel" style="margin-top:1rem">
        <h2>Comparison</h2>
        <table class="compare-table">
          <thead><tr><th>Supplier</th><th>Landed</th><th>Delivery</th><th>Terms</th><th>Profit</th><th>Rec</th></tr></thead>
          <tbody>${rows || "<tr><td colspan=6>No comparison rows yet.</td></tr>"}</tbody>
        </table>
      </section>`;
    dirty = false;
    main.querySelectorAll("input, select").forEach((el) => el.addEventListener("change", () => { dirty = true; }));
    main.querySelector("[data-back]").onclick = () => navigate("quotes");
    document.getElementById("save-quote").onclick = async () => {
      try {
        const res = await api("/api/ui/quotes/" + encodeURIComponent(params.id), {
          method: "POST",
          body: JSON.stringify({
            unit_price: document.getElementById("uq-unit").value ? Number(document.getElementById("uq-unit").value) : null,
            freight: document.getElementById("uq-freight").value ? Number(document.getElementById("uq-freight").value) : null,
            lead_time: document.getElementById("uq-lead").value || null,
            terms: document.getElementById("uq-terms").value || null,
            quote_expiration: document.getElementById("uq-exp").value || null,
            recommendation: document.getElementById("uq-rec").value,
            bucket: "NEEDS REVIEW",
            buyer: (data.deal && data.deal.what && data.deal.what.buyer) || null,
            product: (data.deal && data.deal.what && data.deal.what.product) || null,
          }),
        });
        dirty = false;
        const when = res.saved_at ? new Date(res.saved_at).toLocaleString() : "";
        document.getElementById("save-feedback").textContent = `Saved · ${when}`;
        toast(`Saved · ${when}`);
      } catch (e) {
        toast(e.message);
      }
    };
  }

  async function renderRegistrations() {
    setHint("High-priority portals unlock more buyers.");
    const data = await api("/api/ui/registrations");
    main.innerHTML = `
      <h1>Registrations</h1>
      <p class="lead">Why spend time? Each card shows what you unlock.</p>
      ${(data.items || []).length ? data.items.map((r) => `
        <article class="deal-card">
          <div class="deal-card-top">
            <h3 class="deal-title">${esc(r.portal)}</h3>
            ${priorityBadge(r.priority)}
            <span class="badge yellow">REGISTER FIRST</span>
          </div>
          <div class="meta-row">
            <span>${esc(r.free_or_paid)}</span>
            <span>${esc(r.estimated_time)}</span>
            <span>Unlocks ${esc(r.buyers_unlocked)} buyers</span>
            <span>${esc(r.opportunities_unlocked)} current relevant opportunities</span>
          </div>
          <p class="why">${esc(r.why)}</p>
          <button type="button" class="btn primary" data-reg="${esc(r.portal_id)}">REGISTER NOW</button>
        </article>`).join("") : emptyBox(data.empty)}`;
    main.querySelectorAll("[data-reg]").forEach((b) => {
      b.onclick = () => navigate("registration", { id: b.getAttribute("data-reg") });
    });
  }

  async function renderRegistration(params) {
    const data = await api("/api/ui/registrations");
    const r = (data.items || []).find((x) => x.portal_id === params.id);
    if (!r) {
      main.innerHTML = emptyBox("Registration task not found or already completed.");
      return;
    }
    const steps = ((r.walkthrough || {}).steps || []).map((s) => `
      <li><strong>${esc(s.title)}</strong><div>${s.n === 1 && r.registration_url
        ? `<a href="${esc(r.registration_url)}" target="_blank" rel="noopener">${esc(r.registration_url)}</a>`
        : esc(s.body)}</div></li>`).join("");
    main.innerHTML = `
      <p><button type="button" class="btn ghost" data-back>← Back</button></p>
      <h1>${esc(r.portal)}</h1>
      <p class="lead">${esc(r.why)}</p>
      <section class="panel">
        <h2>Walkthrough</h2>
        <ol>${steps}</ol>
        <div class="field">
          <label for="conf">Confirmation</label>
          <input id="conf" type="text" placeholder="e.g. Registered as vendor #123">
        </div>
        <button type="button" class="btn primary" id="mark-reg">Mark Registered</button>
        <p class="muted" id="save-feedback"></p>
      </section>`;
    main.querySelector("[data-back]").onclick = () => navigate("registrations");
    document.getElementById("mark-reg").onclick = async () => {
      try {
        const res = await api("/api/ui/registrations/" + encodeURIComponent(r.portal_id) + "/mark", {
          method: "POST",
          body: JSON.stringify({ confirmation: document.getElementById("conf").value || null }),
        });
        const when = res.saved_at ? new Date(res.saved_at).toLocaleString() : "";
        document.getElementById("save-feedback").textContent = `Saved · ${when}`;
        toast(`Saved · ${when}`);
        setTimeout(() => navigate("registrations"), 500);
      } catch (e) {
        toast(e.message);
      }
    };
  }

  async function renderBlocked(params) {
    const page = params.page || "1";
    const data = await api("/api/ui/blocked?page=" + page);
    main.innerHTML = `
      <h1>Blocked</h1>
      <p class="lead">These are waiting on access outside the daily call queue — not the same as Watch.</p>
      ${(data.items || []).length ? data.items.map((d) => dealCard(d, { primaryLabel: "VIEW", action: "open" })).join("") : emptyBox(data.empty)}
      <div class="pager">
        <button type="button" class="btn ghost" id="prev" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted">${esc(data.total)} blocked</span>
        <button type="button" class="btn ghost" id="next" ${data.has_more ? "" : "disabled"}>Next</button>
      </div>`;
    bindDealActions();
    document.getElementById("prev").onclick = () => navigate("blocked", { page: String(Math.max(1, data.page - 1)) });
    document.getElementById("next").onclick = () => navigate("blocked", { page: String(data.page + 1) });
  }

  async function renderWatch(params) {
    const page = params.page || "1";
    const data = await api("/api/ui/watch?page=" + page);
    main.innerHTML = `
      <h1>Watch</h1>
      <p class="lead">Future opportunities, weak evidence, or access not worth resolving yet.</p>
      ${(data.items || []).length ? data.items.map((d) => `
        <article class="deal-card">
          <div class="deal-card-top">
            <h3 class="deal-title">${esc(d.buyer)} — ${esc(d.product)}</h3>
            ${badge("WATCH", "gray")}
          </div>
          <p><strong>Why watching:</strong> ${esc(d.why_watching || d.why || "")}</p>
          <p class="muted"><strong>Moves forward when:</strong> ${esc(d.moves_forward_when || "")}</p>
          <button type="button" class="btn secondary" data-act="open" data-id="${esc(d.deal_id)}">OPEN DEAL</button>
        </article>`).join("") : emptyBox(data.empty)}
      <div class="pager">
        <button type="button" class="btn ghost" id="prev" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted">${esc(data.total)} watched</span>
        <button type="button" class="btn ghost" id="next" ${data.has_more ? "" : "disabled"}>Next</button>
      </div>`;
    bindDealActions();
    document.getElementById("prev").onclick = () => navigate("watch", { page: String(Math.max(1, data.page - 1)) });
    document.getElementById("next").onclick = () => navigate("watch", { page: String(data.page + 1) });
  }

  async function renderBidPrep() {
    const data = await api("/api/ui/bid-prep");
    main.innerHTML = `
      <h1>Bid Prep</h1>
      <p class="lead">${esc(data.note || "Production solicitation intake → R1 compliance foundation. Not bid-ready / not submittable yet.")}</p>
      ${(data.items || []).length ? data.items.map((d) => {
        const r1 = d.r1 || null;
        const pc = (r1 && r1.package_plain) || null;
        const overview = r1 ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Solicitation package</h3>
            <div class="meta-row">
              <span>Source: ${esc(r1.authoritative_source || d.submission_method || "—")}</span>
              <span>${esc((r1.plain && r1.plain.documents) || "—")}</span>
              <span>Amendments: ${esc(r1.amendments ?? "—")}</span>
              <span>Package: ${esc(r1.package_status || (d.package_health && d.package_health.status) || "—")}</span>
            </div>
            ${d.package_health ? `<p class="muted">${esc(d.package_health.documents_parsed || 0)}/${esc(d.package_health.documents_found || 0)} parsed · ${esc(d.package_health.buyer_templates || 0)} templates · ${esc(d.package_health.missing_references || 0)} missing refs</p>` : ""}
            ${r1.missing_refs ? `<p class="still-needed"><strong>Missing:</strong> ${esc(r1.missing_refs)}</p>` : ""}
            ${d.operator_status ? `<p><strong>Status:</strong> ${esc(d.operator_status)}</p>` : ""}
          </section>
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Compilation</h3>
            <div class="meta-row">
              <span>Response type: <strong>${esc(r1.response_type || "—")}</strong></span>
              <span>Evaluation: <strong>${esc((r1.evaluation_method || []).join(", ") || "—")}</strong></span>
              <span>Status: <strong>${esc(r1.status || "—")}</strong></span>
            </div>
            <p><strong>Requirements</strong> — ${esc((r1.plain && r1.plain.requirements) || "—")}</p>
            <p><strong>Compliance</strong> — ${esc((r1.plain && r1.plain.compliance) || "—")}</p>
            <p><strong>Questions</strong> — ${esc((r1.plain && r1.plain.questions) || "—")}</p>
            <p class="why"><strong>Next action:</strong> ${esc(r1.next_action || d.next_action || "—")}</p>
          </section>
          ${(d.ocr_review_queue || []).length ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>OCR review needed</h3>
            ${(d.ocr_review_queue || []).slice(0, 5).map((q) => `
              <p><strong>${esc(q.filename || "Document")} — Page ${esc(q.page)}</strong>
              <br/><span class="muted">Confidence: ${esc(q.confidence || "—")}</span>
              <br/>${esc(q.excerpt || "")}</p>`).join("")}
            <p class="muted">Open response project to Confirm / Correct / Mark unreadable.</p>
          </section>` : ""}
          ${(d.amendment_review_queue || []).length ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Amendment review</h3>
            ${(d.amendment_review_queue || []).slice(0, 3).map((q) => `
              <p><strong>Amendment ${esc(q.amendment_number || "?")}</strong> changed ${esc(q.change_count || 0)} item(s)
              <br/>${esc((q.summary || []).join(" · "))}</p>`).join("")}
          </section>` : ""}
          ${d.r2 ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Product / lines</h3>
            <div class="meta-row">
              <span>Lines: <strong>${esc((d.r2.lines && d.r2.lines.count) || 0)}</strong></span>
              <span>Technical: <strong>${esc((d.r2.technical && d.r2.technical.pass) || 0)} PASS</strong> /
                ${esc((d.r2.technical && d.r2.technical.needs_evidence) || 0)} needs evidence /
                ${esc((d.r2.technical && d.r2.technical.fail) || 0)} FAIL</span>
            </div>
            <div class="meta-row">
              <span>Quoted: ${esc((d.r2.supplier_pricing && d.r2.supplier_pricing.quoted_lines) || 0)}</span>
              <span>Needs quote: ${esc((d.r2.supplier_pricing && d.r2.supplier_pricing.needs_quote) || 0)}</span>
            </div>
            ${(d.r2.economics && (d.r2.economics.scenario_bid || d.r2.economics.expected_profit)) ? `
            <p><strong>Economics</strong> (scenario — not final bid)<br/>
              Bid scenario: ${esc(d.r2.economics.scenario_bid || "—")} ·
              Expected profit: ${esc(d.r2.economics.expected_profit || "—")} ·
              Margin: ${esc(d.r2.economics.margin || "—")}% ·
              Evidence: ${esc(d.r2.economics.evidence || "UNKNOWN")}
            </p>` : `<p class="muted">Economics: quote/freight evidence required before verified profit.</p>`}
            ${(d.r2.blockers || []).length ? `<p class="still-needed"><strong>Blockers:</strong> ${esc((d.r2.blockers || []).join("; "))}</p>` : ""}
            <p class="why"><strong>Next action:</strong> ${esc(d.r2.next_action || d.next_action || "—")}</p>
            <p class="muted">Readiness: ${esc(d.r2.readiness || "—")} · never ready-to-submit from R2</p>
          </section>` : ""}
          ${d.r3 ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Company eligibility</h3>
            <div class="meta-row">
              <span>Small Business: <strong>${esc(d.r3.small_business || "—")}</strong></span>
              <span>SAM: <strong>${esc(d.r3.sam || "—")}</strong></span>
              <span>CAGE: <strong>${esc(d.r3.cage || "—")}</strong></span>
            </div>
            <div class="meta-row">
              <span>NMR: <strong>${esc(d.r3.nmr || "—")}</strong></span>
              <span>Trade: <strong>${esc(d.r3.trade || "—")}</strong></span>
              <span>Section 889: <strong>${esc(d.r3.section_889 || "—")}</strong></span>
            </div>
            ${d.r3.section_889_plain ? `<p class="muted">${esc(d.r3.section_889_plain)}</p>` : ""}
            ${(d.r3.registrations && d.r3.registrations.items || []).length ? `
            <p><strong>Registrations</strong>
              · ${esc(d.r3.registrations.hard_blocks || 0)} blockers
              · ${esc(d.r3.registrations.register_before_bid || 0)} register-before-bid
            </p>` : ""}
            ${(d.r3.owner_attestations_open) ? `<p class="still-needed"><strong>Owner confirmation required:</strong> ${esc(d.r3.owner_attestations_open)} open</p>` : ""}
            ${(d.r3.blockers || []).length ? `<p class="still-needed"><strong>Compliance blockers:</strong> ${esc((d.r3.blockers || []).join("; "))}</p>` : ""}
            <p class="why"><strong>Next action:</strong> ${esc(d.r3.next_action || "—")}</p>
            <p class="muted">Readiness: ${esc(d.r3.readiness || "—")} · ${esc(d.r3.disclaimer || "Not a legal compliance certificate.")}</p>
          </section>` : ""}
          ${d.r4 ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Response package <span class="badge gray">${esc(d.r4.label || "DRAFT — NOT SUBMITTED")}</span></h3>
            <div class="meta-row">
              <span>Status: <strong>${esc(d.r4.package_status || "NOT_STARTED")}</strong></span>
              <span>Docs: <strong>${esc(d.r4.documents_generated || 0)}</strong> / ${esc(d.r4.documents_required || 0)}</span>
              <span>Signatures: <strong>${esc(d.r4.owner_signature_required || 0)}</strong></span>
            </div>
            <div class="meta-row">
              <span>Technical matrices: ${esc((d.r4.technical && d.r4.technical.matrices) || 0)}</span>
              <span>Attestations pending: ${esc(d.r4.owner_attestations_pending || 0)}</span>
            </div>
            ${(d.r4.blockers || []).length ? `<p class="still-needed"><strong>Blockers:</strong> ${esc((d.r4.blockers || []).join("; "))}</p>` : ""}
            <p class="why"><strong>Next action:</strong> ${esc(d.r4.next_action || "BUILD RESPONSE PACKAGE")}</p>
            <p class="muted">Never ready-to-submit from R4 · max state READY_FOR_R5_PREFLIGHT</p>
            ${d.response_project_id ? `<button type="button" class="btn primary" data-build-r4="${esc(d.response_project_id)}">BUILD RESPONSE PACKAGE</button>` : ""}
          </section>` : ""}
          ${(d.operator_state || d.r5) ? `
          <section class="panel" style="margin:.5rem 0;padding:.75rem;box-shadow:none">
            <h3>Review &amp; submit</h3>
            <p><strong>Status:</strong> ${esc((d.operator_state && d.operator_state.plain_status) || (d.r5 && d.r5.plain_status) || "—")}</p>
            ${(d.operator_state && d.operator_state.stages) ? `
            <div class="meta-row">${(d.operator_state.stages || []).map((s) =>
              `<span title="${esc(s.state)}">${esc(s.label)}: <strong>${esc(s.state)}</strong></span>`
            ).join("")}</div>` : ""}
            ${d.r5 && d.r5.preflight && d.r5.preflight.summary ? `
            <p>Final checks: <strong>${esc(d.r5.preflight.summary)}</strong>
              ${d.r5.preflight.warnings ? ` · ${esc(d.r5.preflight.warnings)} warnings` : ""}
              ${d.r5.preflight.fails ? ` · ${esc(d.r5.preflight.fails)} blockers` : ""}
            </p>` : ""}
            ${d.r5 && d.r5.approval && d.r5.approval.status ? `
            <p>Owner approval: <strong>${esc(d.r5.approval.status)}</strong>
              ${d.r5.approval.offer ? ` · Offer ${esc(d.r5.approval.offer)}` : ""}
              ${d.r5.approval.expected_profit != null ? ` · Expected profit ${esc(d.r5.approval.expected_profit)} (${esc(d.r5.approval.profit_state || "SCENARIO")})` : ""}
            </p>` : ""}
            ${d.deadline || (d.operator_state && d.operator_state.deadline) ? `
            <p>Due: <strong>${esc(d.deadline || d.operator_state.deadline)}</strong>
              ${esc(d.timezone || (d.operator_state && d.operator_state.timezone) || "")}</p>` : ""}
            <p class="why"><strong>Next:</strong> ${esc(
              (d.operator_state && d.operator_state.ui_next_action_label) ||
              (d.r5 && d.r5.next_action && d.r5.next_action.action_label) ||
              d.next_action || "—"
            )}</p>
            <p class="muted">${esc((d.operator_state && d.operator_state.ui_next_action_reason) || "")}</p>
            <div class="deal-actions">
              ${d.response_project_id ? `<button type="button" class="btn ghost" data-preflight="${esc(d.response_project_id)}">RUN FINAL PREFLIGHT</button>` : ""}
              ${d.response_project_id && d.r5 && d.r5.preflight && d.r5.preflight.status && (d.r5.preflight.status === "PASS" || d.r5.preflight.status === "WARNING") ? `<button type="button" class="btn primary" data-owner-approve="${esc(d.response_project_id)}">APPROVE FOR SUBMISSION</button>` : ""}
            </div>
            <p class="muted">Guided submission only · receipts required · never auto-submit</p>
          </section>` : ""}
        ` : `<p class="muted">No response project yet. Start Bid Prep to fetch/read the solicitation package automatically.</p>`;
        return `
        <article class="deal-card">
          <div class="deal-card-top">
            <h3 class="deal-title">${esc(d.buyer)} — ${esc(d.product)}</h3>
            ${badge(d.plain_status || d.operator_status || d.ui_status, d.ui_status_color)}
            ${urgencyBadge(d.deadline_urgency)}
          </div>
          ${overview}
          <ul>${(d.checklist || []).map((c) => `<li>${c.done ? "✓" : "○"} ${esc(c.label)}</li>`).join("")}</ul>
          <p class="muted">Missing: ${esc((d.missing_items || []).join(", ") || "—")}</p>
          <div class="deal-actions">
            <button type="button" class="btn primary" data-start-bid="${esc(d.canonical_id || (d.deal_id || "").replace(/^c:/, ""))}" data-deal="${esc(d.deal_id)}">${esc(d.primary_action || "START BID PREP")}</button>
            <button type="button" class="btn ghost" data-act="open" data-id="${esc(d.deal_id)}">OPEN DEAL</button>
          </div>
          ${d.response_project_id ? `<p class="muted">Project ${esc(d.response_project_id)} · Draft until owner approval + receipt</p>` : ""}
        </article>`;
      }).join("") : emptyBox(data.empty)}`;
    bindDealActions();
    main.querySelectorAll("[data-start-bid]").forEach((b) => {
      b.onclick = async () => {
        const cid = b.getAttribute("data-start-bid");
        try {
          toast("Fetching solicitation…");
          const res = await api("/api/response-projects/from-opportunity/" + encodeURIComponent(cid), {
            method: "POST",
            body: JSON.stringify({ compile: true, documents: [] }),
          });
          const s = (res && res.operator_summary) || {};
          toast(`Solicitation compiled · ${s.requirements_found || 0} requirements · ${s.hard_blockers || 0} blockers`);
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Could not start Bid Prep");
        }
      };
    });
    main.querySelectorAll("[data-build-r4]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-build-r4");
        try {
          toast("Building response package (draft)…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/generate", {
            method: "POST",
            body: JSON.stringify({}),
          });
          const pkg = (res && res.package) || {};
          toast(`Draft package ${pkg.package_status || "built"} · ${(pkg.generated_documents || []).length} docs · NOT SUBMITTED`);
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Could not build response package");
        }
      };
    });
    main.querySelectorAll("[data-preflight]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-preflight");
        try {
          toast("Running final preflight…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/preflight", {
            method: "POST",
            body: JSON.stringify({}),
          });
          toast(res.summary || res.plain || "Preflight complete");
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Preflight failed");
        }
      };
    });
    main.querySelectorAll("[data-owner-approve]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-owner-approve");
        try {
          toast("Recording owner approval…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/owner-approval", {
            method: "POST",
            body: JSON.stringify({ decision: "APPROVED", approved_by: "owner" }),
          });
          toast(res.ok ? "Approved for guided submission" : (res.error || "Approval failed"));
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Approval failed");
        }
      };
    });
  }

  async function renderSettings() {
    const data = await api("/api/ui/settings");
    const prev = await api("/api/ui/preservation");
    let compliance = null;
    try {
      compliance = await api("/api/company-compliance-profile");
    } catch (_) {
      compliance = null;
    }
    const p = (compliance && compliance.profile) || {};
    const complete = p.completeness || {};
    main.innerHTML = `
      <h1>Settings</h1>
      <section class="panel">
        <h2>Operator</h2>
        <label class="train-toggle"><input type="checkbox" id="set-train" ${training ? "checked" : ""}> Training Mode (short hints)</label>
        <p class="muted" style="margin-top:.75rem">${esc(data.advanced_note || "")}</p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Company compliance</h2>
        <p class="muted">Source-backed profile. UNKNOWN is correct when unverified. Never invents certifications. Live SAM API: off.</p>
        <div class="meta-row">
          <span>Entity: <strong>${esc(p.legal_name || "UNKNOWN")}</strong></span>
          <span>UEI: <strong>${esc(p.UEI || "UNKNOWN")}</strong></span>
          <span>CAGE: <strong>${esc(p.CAGE || "UNKNOWN")}</strong></span>
        </div>
        <div class="meta-row">
          <span>SAM: <strong>${esc(p.SAM_registration_status || "UNKNOWN")}</strong></span>
          <span>Size: <strong>${esc(p.small_business_status || "UNKNOWN")}</strong></span>
          <span>JCP: <strong>${esc(p.jcp_status || "UNKNOWN")}</strong></span>
        </div>
        <p>Known: ${esc((complete.required_known || []).join(", ") || "—")} · Unknown: ${esc((complete.required_unknown || []).join(", ") || "—")}</p>
        ${(p.conflicts || []).length ? `<p class="still-needed"><strong>Conflicts:</strong> ${(p.conflicts || []).map((c) => esc(c.field + ": SAM vs profile")).join("; ")}</p>` : ""}
        <p class="muted">Profile version ${esc(p.profile_version || "—")} · Edit via data/company_eligibility_profile.json (Settings write-back is R4+).</p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Docs</h2>
        <ul>
          <li><a href="/docs/M3_OPERATOR_QUICKSTART.md" target="_blank">Operator Quickstart</a></li>
          <li><a href="/docs/M3_OPERATOR_CHEATSHEET.md" target="_blank">One-page cheat sheet</a></li>
          <li><a href="/docs/M3_OPERATOR_TRAINING_TEST.md" target="_blank">Training test</a></li>
        </ul>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>System <span class="badge gray">Advanced</span></h2>
        <p class="muted">Build ${esc(BUILD)}</p>
        <p>Ready-to-call preserved: <strong>${esc(prev.ready_to_call)}</strong> · Canonical rows: <strong>${esc(prev.canonical_rows)}</strong></p>
        <p><a href="/supplier_call_desk.html">Legacy call desk artifact view</a> · <a href="/index.html?legacy=1">Legacy research UI</a></p>
      </section>`;
    document.getElementById("set-train").onchange = (e) => {
      training = e.target.checked;
      trainingToggle.checked = training;
      localStorage.setItem("m3_training_mode", training ? "1" : "0");
      setHint(training ? "Training Mode on." : "");
    };
  }

  async function renderSearch(params) {
    const data = await api("/api/ui/search?q=" + encodeURIComponent(params.q || ""));
    main.innerHTML = `
      <h1>Search</h1>
      <p class="muted">Results for “${esc(params.q || "")}”</p>
      ${(data.items || []).length ? data.items.map((i) => `
        <article class="deal-card">
          <h3 class="deal-title">${esc(i.label)}</h3>
          <p class="muted">${esc(i.sub || i.type)}</p>
          <button type="button" class="btn primary" data-act="open" data-id="${esc(i.deal_id)}">OPEN</button>
        </article>`).join("") : emptyBox("No matches.")}`;
    bindDealActions();
  }

  async function render() {
    const { path, params } = parseHash();
    route = path;
    document.querySelectorAll(".nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.route === path);
    });
    main.innerHTML = `<p class="muted">Loading…</p>`;
    try {
      if (path === "home") await renderHome();
      else if (path === "today") await renderToday(params);
      else if (path === "deals") await renderDeals(params);
      else if (path === "deal") await renderDeal(params);
      else if (path === "call" || path === "calls" && params.id) await renderCall(params);
      else if (path === "calls") await renderCalls();
      else if (path === "quotes") await renderQuotes();
      else if (path === "quote") await renderQuote(params);
      else if (path === "registrations") await renderRegistrations();
      else if (path === "registration") await renderRegistration(params);
      else if (path === "blocked") await renderBlocked(params);
      else if (path === "watch") await renderWatch(params);
      else if (path === "bid-prep") await renderBidPrep();
      else if (path === "settings") await renderSettings();
      else if (path === "search") await renderSearch(params);
      else await renderHome();
    } catch (e) {
      main.innerHTML = `<div class="empty"><strong>Could not load this screen.</strong><p>${esc(e.message)}</p>
        <button type="button" class="btn primary" id="retry">Retry</button></div>`;
      document.getElementById("retry").onclick = () => render();
    }
  }

  document.querySelectorAll(".nav button, .brand").forEach((el) => {
    el.addEventListener("click", () => navigate(el.dataset.route || "home"));
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        navigate(el.dataset.route || "home");
      }
    });
  });

  trainingToggle.addEventListener("change", () => {
    training = trainingToggle.checked;
    localStorage.setItem("m3_training_mode", training ? "1" : "0");
    setHint(training ? "Training Mode on — short hints only." : "");
  });

  document.getElementById("btn-walkthrough").onclick = () => walkthroughDlg.showModal();
  document.getElementById("walkthrough-start").onclick = (e) => {
    e.preventDefault();
    walkthroughDlg.close();
    localStorage.setItem("m3_walkthrough_done", "1");
    walkthroughDone = true;
    training = true;
    trainingToggle.checked = true;
    localStorage.setItem("m3_training_mode", "1");
    navigate("today");
  };

  document.getElementById("global-search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = document.getElementById("global-search").value.trim();
    if (q) navigate("search", { q });
  });

  window.addEventListener("hashchange", () => {
    if (!confirmLeave()) {
      history.pushState(null, "", `#/${route}`);
      return;
    }
    dirty = false;
    render();
  });

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  if (!walkthroughDone) {
    setTimeout(() => {
      try { walkthroughDlg.showModal(); } catch (_) {}
    }, 400);
  }

  if (!location.hash) location.hash = "#/home";
  render();
})();
