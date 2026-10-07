/* M3 Operator Console — guided work queue (no phase architecture in UI) */
(function () {
  "use strict";

  const BUILD = "20261005-m3-bidnet-full-production-v1";
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
      b.classList.toggle("active", b.dataset.route === next || (next.startsWith("deal") && b.dataset.route === "deals") || (next === "call" && b.dataset.route === "calls") || (next === "financing" && b.dataset.route === "financing"));
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
    let finStrip = "";
    try {
      const fin = await api("/api/financing/dashboard");
      const cap = fin.capital || {};
      const blocked = (fin.blocked_profit || {}).total_blocked || "0.00";
      finStrip = `<div class="summary-strip" aria-label="Financing summary" style="margin-top:.75rem">
        <span>Confirmed cash <strong>$${esc(fin.confirmed_company_capital || cap.business_cash || "0")}</strong></span>
        <span>Deployable <strong>$${esc(fin.deployable_capital || cap.deployable_capital || "0")}</strong></span>
        <span>Reserved <strong>$${esc(fin.capital_reserved || "0")}</strong></span>
        <span>Likely <strong>${esc((fin.status_counts || {}).likely_financeable || 0)}</strong></span>
        <span>Gaps <strong>${esc((fin.status_counts || {}).financing_gaps || 0)}</strong></span>
        <span>Exec fail <strong>${esc((fin.status_counts || {}).execution_failed || 0)}</strong></span>
        <span>Blocked profit <strong>$${esc(blocked)}</strong></span>
        <button type="button" class="btn ghost" data-route-fin style="margin-left:.5rem">Financing</button>
      </div>`;
    } catch (_) { /* optional */ }
    const health = data.data_health || {};
    const healthMissing = health.status === "DATA_SOURCE_MISSING";
    const dh = data.discovery_health || {};
    const dhStatus = dh.run_status || "NEVER_RUN";
    const srcOk = dh.sources_succeeded != null ? dh.sources_succeeded : "—";
    const srcAttempt = dh.sources_attempted != null ? dh.sources_attempted : "—";
    const samUsed = dh.sam_calls_used_today != null ? dh.sam_calls_used_today : "—";
    const samLimit = dh.sam_calls_limit != null ? dh.sam_calls_limit : 10;
    const lastRunLabel = dh.last_attempted_run || dh.last_successful_daily_run || "Never";
    const discoveryStrip = `<div class="summary-strip" aria-label="Discovery health" style="margin-top:.75rem">
      <span>Discovery <strong>${esc(dhStatus)}</strong></span>
      <span>Last run <strong>${esc(lastRunLabel)}</strong></span>
      <span>New <strong>${esc(dh.new_opportunities_added_today ?? 0)}</strong></span>
      <span>Updated <strong>${esc(dh.opportunities_updated_today ?? 0)}</strong></span>
      <span>Expired <strong>${esc(dh.opportunities_expired_removed_today ?? 0)}</strong></span>
      <span>Available <strong>${esc(dh.currently_available ?? health.currently_available ?? "—")}</strong></span>
      <span>Sources <strong>${esc(srcOk)}/${esc(srcAttempt)}</strong></span>
      <span>SAM <strong>${esc(samUsed)}/${esc(samLimit)}</strong></span>
      <button type="button" class="btn ghost" id="btn-run-discovery" style="margin-left:.5rem">Run Discovery Now</button>
    </div>`;
    const healthStrip = `<div class="summary-strip" aria-label="Opportunity data health" style="margin-top:.75rem">
      <span>Opportunity data <strong>${esc(health.status || "—")}</strong></span>
      <span>Canonical <strong>${esc(health.canonical_count == null ? "—" : health.canonical_count)}</strong></span>
      <span>Available <strong>${esc(health.currently_available == null ? "—" : health.currently_available)}</strong></span>
      <span>Unlocks <strong>${esc(health.registration_unlocks == null ? "—" : health.registration_unlocks)}</strong></span>
      <span>Unique blocked <strong>${esc(health.unique_blocked_opportunities == null ? "—" : health.unique_blocked_opportunities)}</strong></span>
      ${healthMissing ? `<span class="badge red">Store missing</span>` : ""}
    </div>`;
    const cards = (data.cards || []).map((c) => `
      <button type="button" class="stat-card ${esc(c.color)}" data-go="${esc(c.id)}">
        <span class="label">${esc(c.label)}</span>
        <span class="count">${esc(c.count)}</span>
      </button>`).join("");
    const s = data.summary || {};
    main.innerHTML = `
      <h1>Home</h1>
      <p class="lead">${esc(data.headline || "What needs my attention today?")}</p>
      ${healthMissing ? `<div class="still-needed"><strong>Opportunity data store missing</strong><p>${esc(health.message || data.next_useful || "")}</p></div>` : ""}
      ${data.caught_up && !healthMissing ? `<div class="caught-up"><h2>You're caught up.</h2><p>${esc(data.next_useful || "")}</p></div>` : ""}
      <div class="home-grid">${cards}</div>
      <div class="summary-strip" aria-label="Business summary">
        <span>Active deals <strong>${esc(s.active_deals)}</strong></span>
        <span>Calls due <strong>${esc(s.calls_due)}</strong></span>
        <span>Quotes pending <strong>${esc(s.quotes_pending)}</strong></span>
        <span>Bid-ready <strong>${esc(s.bid_ready)}</strong></span>
        <span class="muted">${esc(s.note || "")}</span>
      </div>
      ${discoveryStrip}
      ${healthStrip}
      ${finStrip}
      <p style="margin-top:1rem">
        <button type="button" class="btn primary" data-route="today">Open Today's Work</button>
        <button type="button" class="btn secondary" data-route-deals style="margin-left:.5rem">Available Deals</button>
      </p>
    `;
    main.querySelectorAll("[data-go]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-go");
        if (id === "quotes") navigate("quotes");
        else if (id === "registrations") navigate("registrations");
        else if (id === "bid_prep") navigate("bid-prep");
        else if (id === "blocked") navigate("blocked");
        else if (id === "available") navigate("deals", { filter: "available" });
        else navigate("today", { section: id });
      });
    });
    main.querySelector("[data-route=today]")?.addEventListener("click", () => navigate("today"));
    main.querySelector("[data-route-deals]")?.addEventListener("click", () => navigate("deals", { filter: "available" }));
    main.querySelector("[data-route-fin]")?.addEventListener("click", () => navigate("financing"));
    main.querySelector("#btn-run-discovery")?.addEventListener("click", async () => {
      const btn = main.querySelector("#btn-run-discovery");
      if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
      try {
        const res = await api("/api/ui/discovery/run", { method: "POST", body: "{}" });
        if (res.already_running) {
          setHint("Discovery already running.");
        } else if (res.accepted) {
          setHint("Discovery started: " + (res.run_id || "ok"));
        } else {
          setHint(res.message || "Discovery not started.");
        }
        setTimeout(() => renderHome(), 2500);
      } catch (e) {
        setHint(e.message || "Discovery run failed to start");
        if (btn) { btn.disabled = false; btn.textContent = "Run Discovery Now"; }
      }
    });
  }

  function channelFitCard(row) {
    const cf = row.channel_fit || row;
    const bs = cf.bidder_structure || {};
    const hist = `Dist ${esc(bs.DISTRIBUTOR_BIDDER_PERCENT ?? "—")}% · Reseller ${esc(bs.RESELLER_BIDDER_PERCENT ?? "—")}%`;
    return `<article class="deal-card">
      <h3>${esc(cf.title || row.title || row.opportunity || "—")}</h3>
      <p class="muted">${esc(cf.buyer || row.buyer || "")} · Deadline ${esc(cf.deadline || row.deadline || "—")} · ${esc(cf.days_remaining ?? "—")} days</p>
      <p>Gov ${esc(cf.government_value ?? "—")} · Public basket ${esc(cf.PUBLIC_BASKET_VALUE ?? "—")} · Coverage ${esc(cf.PUBLIC_PRICE_COVERAGE_CLASS || "—")} (${esc(cf.PUBLIC_BASKET_VALUE_COVERAGE ?? cf.PUBLIC_BASKET_LINE_COVERAGE ?? "—")}%)</p>
      <p>Headroom ${esc(cf.VISIBLE_HEADROOM ?? "—")} (${esc(cf.VISIBLE_HEADROOM_PERCENT ?? "—")}%) · Channel ${esc(cf.CHANNEL_COMPETITION_CLASS || "—")} · Dom ${esc(cf.CHANNEL_DOMINANCE_SCORE ?? "—")}</p>
      <p>Bidders: ${hist} · Priority ${esc(cf.QUOTE_PRIORITY_SCORE ?? "—")} · <strong>${esc(cf.PRE_QUOTE_DECISION || "—")}</strong></p>
      <p class="muted">${esc((cf.decision_reasons || []).join("; ") || cf.likely_supplier_strategy || "")}</p>
      <p><strong>Next:</strong> ${esc(cf.next_action || "REVIEW")}</p>
    </article>`;
  }

  async function renderToday(params) {
    setHint("Work actionable BidNet money deals and CALL TODAY items.");
    const [data, money] = await Promise.all([
      api("/api/ui/today"),
      api("/api/m3/bidnet-money/today").catch(() => ({ NEW_ACTIONABLE_DEALS_TODAY: 0, TARGET: 10 })),
    ]);
    const focus = params.section || "call_today";
    const order = ["call_today", "follow_up", "quotes", "registrations", "owner_actions", "responses", "bid_prep", "submissions", "awaiting_result", "blocked"];
    const actionable = money.actionable_now || [];
    const quoteReady = money.ready_for_quote || [];
    const cq = money.channel_queues || {};
    const cs = money.channel_summary || {};
    let html = `<h1>Today's Work</h1><p class="lead">Channel-fit + MSRP screen first — then calls, quotes, registrations.</p>
      <div class="meta-row">
        <span>CALL TODAY: <strong>${esc(cs.CALL_TODAY ?? (cq.CALL_TODAY || []).length)}</strong></span>
        <span>QUOTE IF CAPACITY: <strong>${esc(cs.QUOTE_IF_CAPACITY ?? (cq.QUOTE_IF_CAPACITY || []).length)}</strong></span>
        <span>WATCH: <strong>${esc(cs.WATCH ?? (cq.WATCH || []).length)}</strong></span>
        <span>PASS: <strong>${esc(cs.PASS ?? (cq.PASS || []).length)}</strong></span>
        <span>INSUFFICIENT: <strong>${esc(cs.INSUFFICIENT_EVIDENCE ?? (cq.INSUFFICIENT_EVIDENCE || []).length)}</strong></span>
        <span>Money actionable: <strong>${esc(money.NEW_ACTIONABLE_DEALS_TODAY ?? 0)}</strong> / ${esc(money.TARGET ?? 10)}</span>
      </div>`;
    const channelSections = [
      ["CALL_TODAY", "CALL TODAY"],
      ["QUOTE_IF_CAPACITY", "QUOTE IF CAPACITY"],
      ["WATCH", "WATCH"],
      ["PASS", "PASS"],
    ];
    for (const [key, title] of channelSections) {
      const items = cq[key] || [];
      if (!items.length && key !== "CALL_TODAY") continue;
      html += `<section class="section"><div class="section-head"><h2>${esc(title)}</h2><span class="muted">${items.length}</span></div>
        ${items.length ? items.slice(0, 15).map(channelFitCard).join("") : `<div class="empty">No ${esc(title)} opportunities scored yet.</div>`}
      </section>`;
    }
    if (actionable.length) {
      html += `<section class="section"><div class="section-head"><h2>Actionable Now</h2><span class="muted">${actionable.length}</span></div>
        ${actionable.slice(0, 10).map((d) => `<article class="deal-card"><h3>${esc(d.title || d.opportunity || "—")}</h3>
          <p class="muted">${esc(d.buyer || "")} · Deadline ${esc(d.deadline || "—")} · ${esc(d.days_remaining ?? "—")} days</p>
          <p>Value ${esc(d.revenue_value ?? "—")} · Cost ${esc(d.our_cost ?? "—")} · Profit ${esc(d.expected_profit ?? "—")}</p>
          <p><strong>Next:</strong> ${esc(d.next_action || "REVIEW")}</p></article>`).join("")}
      </section>`;
    }
    if (quoteReady.length) {
      html += `<section class="section"><div class="section-head"><h2>Ready for Supplier Quote</h2><span class="muted">${quoteReady.length}</span></div>
        ${quoteReady.slice(0, 10).map((d) => `<article class="deal-card"><h3>${esc(d.title || d.opportunity || "—")}</h3>
          <p class="muted">${esc(d.buyer || "")} · ${esc(d.deadline || "—")} · Lines ${esc(d.material_lines ?? "—")} · A–E ${esc(d.usable_ae ?? "—")}</p>
          <p><strong>Next:</strong> ${esc(d.next_action || "REQUEST_QUOTES")} · Supplier ${esc(d.supplier || "—")}</p></article>`).join("")}
      </section>`;
    }
    if (data.caught_up && !actionable.length && !quoteReady.length) {
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
    setHint("Browse currently available opportunities. Unknown economics stay visible.");
    const filter = params.filter || "available";
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.status) qs.set("status", params.status);
    if (params.page) qs.set("page", params.page);
    qs.set("filter", filter);
    qs.set("page_size", "40");
    const data = await api("/api/ui/deals?" + qs.toString());
    if (data.data_status === "DATA_SOURCE_MISSING") {
      main.innerHTML = `
        <h1>Available Deals</h1>
        <div class="still-needed">
          <strong>Opportunity store missing</strong>
          <p>${esc(data.message || "Not a legitimate empty population.")}</p>
          <p class="muted">Path: ${esc(data.store_path || "—")}</p>
          <p>Set <code>M3_DATA_ROOT</code> to a persistent Railway volume that contains the canonical store.</p>
        </div>`;
      return;
    }
    const filters = [
      ["available", "All available"],
      ["product_resale", "Product resale"],
      ["common_commercial", "Common commercial"],
      ["ready_to_research", "Ready to research"],
      ["ready_to_call", "Ready to call"],
      ["ready_to_quote", "Ready to quote"],
      ["registration_blocked", "Registration blocked"],
      ["financing_blocked", "Financing blocked"],
      ["federal", "Federal"],
      ["state_local", "State/local"],
      ["all", "Include rejected"],
    ];
    main.innerHTML = `
      <h1>Available Deals</h1>
      <p class="lead">Current opportunities that are not expired, canceled, or hard-rejected. Unknown pricing/financing still appear.</p>
      ${(data.quote_outreach_reserve_count != null && data.quote_outreach_reserve_count > 0) ? `
      <p class="muted" id="quote-reserve-chip" title="Collapsed by default — only surface when strong pipeline is thin">
        Quote Outreach Reserve: <strong>${esc(data.quote_outreach_reserve_count)}</strong>
        ${data.quote_outreach_reserve_surfaced ? " · surfaced (pipeline thin)" : " · hidden from main list"}
      </p>` : ""}
      <div class="filters">
        <input id="f-q" type="search" placeholder="Search…" value="${esc(params.q || "")}">
        <select id="f-filter">${filters.map(([v,l]) =>
          `<option value="${esc(v)}" ${filter === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
        <select id="f-status">
          <option value="">All statuses</option>
          ${["CALL SUPPLIER","FOLLOW UP","WAITING FOR QUOTE","QUOTE RECEIVED","REGISTER FIRST","BID PREP","WATCH","BLOCKED","SKIP"].map((s) =>
            `<option value="${esc(s)}" ${params.status === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select>
        <button type="button" class="btn secondary" id="f-go">Apply</button>
      </div>
      <p class="muted">${esc(data.total)} shown · ${esc(data.canonical_count || "—")} canonical</p>
      ${(data.items || []).length ? data.items.map((d) => dealCard({
        ...d,
        why: d.next_owner_action || d.why,
        plain: [
          d.jurisdiction_bucket || d.jurisdiction || "UNKNOWN",
          "Class: " + (d.product_service_classification || "UNKNOWN"),
          "Value: " + (d.estimated_value == null || d.estimated_value === "UNKNOWN" ? "UNKNOWN" : ("$" + d.estimated_value)),
          "Profit: " + (d.potential_profit == null || d.potential_profit === "UNKNOWN" ? "UNKNOWN" : ("$" + d.potential_profit)),
          "Supplier: " + (d.supplier_status || "UNKNOWN"),
          "Financing: " + (d.financing_status || "UNKNOWN"),
          d.registration_needed ? "Registration needed: YES" : "Registration needed: NO",
        ].join(" · "),
      })).join("") : emptyBox("No deals match these filters.")}
      <div class="pager">
        <button type="button" class="btn ghost" id="prev" ${data.page <= 1 ? "disabled" : ""}>Previous</button>
        <span class="muted">Page ${esc(data.page)} · ${esc(data.total)} total</span>
        <button type="button" class="btn ghost" id="next" ${data.has_more ? "" : "disabled"}>Next</button>
      </div>`;
    bindDealActions();
    document.getElementById("f-go").onclick = () => navigate("deals", {
      q: document.getElementById("f-q").value,
      status: document.getElementById("f-status").value,
      filter: document.getElementById("f-filter").value,
      page: "1",
    });
    document.getElementById("prev").onclick = () => navigate("deals", { ...params, filter, page: String(Math.max(1, (data.page || 1) - 1)) });
    document.getElementById("next").onclick = () => navigate("deals", { ...params, filter, page: String((data.page || 1) + 1) });
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
            <h2>Canonical workflow</h2>
            <p class="muted">One operator path · stage → next action</p>
            <div class="meta-row">
              <span>Stage: <strong>${esc((data.canonical_funnel || {}).current_stage || "OPPORTUNITY")}</strong></span>
              <span>Next: <strong>${esc(((data.canonical_funnel || {}).next_action) || (next.action) || "—")}</strong></span>
              <span>Revenue: <strong>${esc((data.canonical_funnel || {}).revenue_status || "—")}</strong></span>
              <span>Basket: <strong>${esc((data.canonical_funnel || {}).basket_status || "—")}</strong></span>
              <span>Economics: <strong>${esc((data.canonical_funnel || {}).economics_status || "—")}</strong></span>
              <span>Quotes: <strong>${esc((data.canonical_funnel || {}).quote_packet_status || "—")}</strong></span>
            </div>
          </section>
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
          <section class="panel" id="package-product-data">
            <h2>Product pipeline status</h2>
            ${(() => {
              const pp = data.package_and_product_data || {};
              const pipe = pp.product_pipeline_status || {};
              const cov = pp.extraction_coverage != null
                ? (Number(pp.extraction_coverage) <= 1
                    ? Math.round(Number(pp.extraction_coverage) * 100) + "%"
                    : esc(pp.extraction_coverage))
                : "—";
              const pubCov = pp.public_price_coverage_pct != null
                ? Math.round(Number(pp.public_price_coverage_pct)) + "%"
                : "—";
              const head = pp.visible_headroom != null
                ? ("$" + Number(pp.visible_headroom).toLocaleString() +
                   (pp.visible_headroom_percent != null ? ` (${pp.visible_headroom_percent}%)` : ""))
                : "—";
              return `<p><strong>Product data status:</strong> ${esc(pp.product_data_status || pp.blocker_plain || "Not assessed yet.")}</p>
              <div class="meta-row">
                <span>Package: <strong>${esc(pp.package_label || pipe.package || "Unknown")}</strong></span>
                <span>Documents: <strong>${esc(pp.documents_acquired ?? "—")}</strong> of ${esc(pp.documents_discovered ?? "—")} acquired</span>
                <span>Product document: <strong>${esc(pp.product_schedule || "—")}${pp.authoritative_doc ? " · " + esc(pp.authoritative_doc) : ""}</strong></span>
                <span>Source: <strong>${esc(pp.source_pages || "—")}</strong></span>
              </div>
              <div class="meta-row">
                <span>Lines: <strong>${esc(pp.expected_lines ?? "—")}</strong> expected / <strong>${esc(pp.lines_extracted ?? "—")}</strong> extracted · coverage <strong>${cov}</strong></span>
                <span>Identity: <strong>${esc(pp.identity_ready_lines ?? 0)}</strong> A–E / <strong>${esc(pp.identity_unresolved ?? "—")}</strong> unresolved</span>
              </div>
              <div class="meta-row">
                <span>Government value: <strong>${esc(pp.government_value || "Missing")}</strong></span>
                <span>Public pricing: <strong>${esc(pp.public_priced_lines ?? 0)}</strong> lines · <strong>${pubCov}</strong> coverage${pp.public_basket_value != null ? ` · basket $${esc(Number(pp.public_basket_value).toLocaleString())}` : ""}</span>
                <span>Channel: <strong>${esc(pp.channel_class || "UNKNOWN")}</strong></span>
                <span>Visible headroom: <strong>${esc(head)}</strong></span>
              </div>
              <p><strong>Decision:</strong> ${esc(pp.decision || "—")}</p>
              <p><strong>Current blocker:</strong> ${esc(pp.blocker_plain || "Not assessed yet.")}</p>`;
            })()}
          </section>
          <section class="panel" id="profit-first-card">
            <h2>Profit check</h2>
            <p class="muted">Loading…</p>
          </section>
          <section class="panel" id="line-econ-card">
            <h2>Line-item economics</h2>
            <p class="muted">Loading retail basket…</p>
          </section>
          <section class="panel" id="financing-card">
            <h2>Financing</h2>
            <p class="muted">Checking capital stack…</p>
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
    fillProfitFirstCard(data);
    fillLineItemEconomics(data);
    fillFinancingCard(data);
  }

  function fillProfitFirstCard(deal) {
    const el = document.getElementById("profit-first-card");
    if (!el) return;
    const pf = deal.profit_first;
    if (!pf || !pf.owner_card) {
      el.innerHTML = `<h2>Profit check</h2>
        <p class="muted">Not enough evidence yet to judge executable profit.</p>`;
      return;
    }
    const c = pf.owner_card;
    const unk = (c.still_unknown || []).length
      ? `<p class="muted">Still unknown: ${esc((c.still_unknown || []).join(", "))}</p>`
      : "";
    el.innerHTML = `<h2>Profit check</h2>
      <div class="meta-row">
        <span>Expected revenue <strong>${money(c.expected_revenue)}</strong></span>
        <span>Product cost <strong>${money(c.product_cost)}</strong></span>
        <span>Freight <strong>${money(c.freight)}</strong></span>
        <span>Financing <strong>${money(c.financing)}</strong></span>
        <span>Other <strong>${money(c.other)}</strong></span>
      </div>
      <p style="font-size:1.15rem;margin:.5rem 0">Expected profit: <strong>${money(c.expected_profit)}</strong></p>
      <div class="meta-row">
        <span>Price basis: <strong>${esc(c.price_basis || "—")}</strong></span>
        <span>Proof: <strong>${esc(c.proof || pf.profit_status || "—")}</strong></span>
        <span>Completeness: <strong>${esc(c.completeness == null ? "—" : c.completeness + "%")}</strong></span>
        <span>Execution: <strong>${esc(c.execution || "—")}</strong></span>
      </div>
      <p>Next: <strong>${esc(c.next || "—")}</strong></p>
      ${unk}`;
  }

  function money(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return esc(v);
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function fillLineItemEconomics(deal) {
    const el = document.getElementById("line-econ-card");
    if (!el) return;
    const lie = deal.line_item_economics;
    if (!lie || !lie.owner_summary) {
      el.innerHTML = `<h2>Line-item economics</h2>
        <p class="muted">${esc((lie && lie.message) || "No multi-line retail basket analysis yet. Run analysis when a bid schedule is available.")}</p>`;
      return;
    }
    const s = lie.owner_summary || {};
    const roll = lie.rollup || {};
    const rows = (lie.line_table || []).slice(0, 60);
    const table = rows.length ? `<div style="overflow:auto;max-height:320px;margin-top:.75rem">
      <table class="simple-table" style="width:100%;font-size:.85rem">
        <thead><tr>
          <th>Line</th><th>Item</th><th>Qty</th><th>Retail Unit</th><th>Retail Total</th>
          <th>Gov Hist Unit</th><th>Gov Hist Total</th><th>Spread</th><th>Match</th>
        </tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td>${esc(r.line || "")}</td>
          <td>${esc(r.item || "")}</td>
          <td>${esc(r.qty ?? "")}</td>
          <td>${money(r.retail_unit)}</td>
          <td>${money(r.retail_total)}</td>
          <td>${money(r.gov_hist_unit)}</td>
          <td>${money(r.gov_hist_total)}</td>
          <td>${money(r.spread)}</td>
          <td>${esc(r.match || "")}${r.unresolved ? " · unresolved" : ""}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : "";
    const cov = lie.supplier_coverage || {};
    const oneStop = (cov.one_stop_best)
      ? `<p class="muted">One-stop: <strong>${esc(cov.one_stop_best.supplier)}</strong> covers ${esc(cov.one_stop_best.lines_covered)}/${esc(cov.total_lines)} (${esc(cov.one_stop_coverage_pct)}%)</p>`
      : "";
    const disc = roll.required_discounts || {};
    el.innerHTML = `<h2>Line-item economics</h2>
      <div class="meta-row">
        <span>Lines: <strong>${esc(s.lines ?? "—")}</strong></span>
        <span>Priced: <strong>${esc(s.priced ?? "—")}</strong></span>
        <span>Historical: <strong>${esc(s.historical_matched ?? "—")}</strong></span>
        <span>Status: <strong>${esc(s.status || "—")}</strong></span>
        <span>Confidence: <strong>${esc(s.confidence || "—")}</strong></span>
      </div>
      <div class="meta-row">
        <span>Retail cost: <strong>${money(s.retail_cost)}</strong></span>
        <span>Hist gov value: <strong>${money(s.historical_gov_value)}</strong></span>
        <span>Spread: <strong>${money(s.retail_spread)}</strong></span>
        <span>Freight: <strong>${money(s.estimated_freight)}</strong></span>
        <span>Post-financing: <strong>${money(s.post_financing_profit)}</strong></span>
      </div>
      <p>Next: <strong>${esc(s.next_action || lie.next_action || "—")}</strong>
        · Simple-resale: <strong>${esc(s.simple_resale_score ?? (lie.simple_resale || {}).simple_resale_score ?? "—")}</strong>
        · Proof: <strong>${esc(s.proof_label || roll.proof_label || "—")}</strong></p>
      <p class="muted">Discount need — break-even ${esc(disc.break_even ?? "—")}% · $5K ${esc(disc.profit_5000 ?? "—")}% · $10K ${esc(disc.profit_10000 ?? "—")}%</p>
      ${oneStop}
      ${table}`;
  }

  async function fillFinancingCard(deal) {
    const el = document.getElementById("financing-card");
    if (!el) return;
    const w = deal.what || {};
    const econ = deal.economics || deal.why_we_care || {};
    const cv = econ.contract_value || econ.estimated_value || w.estimated_value || deal.contract_value;
    const sc = econ.supplier_cost || econ.acquisition_cost || deal.supplier_cost;
    const fr = econ.freight || deal.freight || 0;
    if (cv == null || sc == null || sc === "" || sc === "UNKNOWN") {
      el.innerHTML = `<h2>Financing</h2>
        <p><strong>Status: FINANCING UNKNOWN</strong></p>
        <p class="muted">Need supplier cost before financing analysis.</p>
        <button type="button" class="btn ghost" data-go-fin>Open Financing Intelligence</button>`;
      el.querySelector("[data-go-fin]")?.addEventListener("click", () => navigate("financing"));
      return;
    }
    try {
      const res = await api("/api/financing/opportunities/" + encodeURIComponent(deal.deal_id) + "/assess", {
        method: "POST",
        body: JSON.stringify({
          contract_value: cv,
          supplier_cost: sc,
          freight: fr,
          supplier_terms: deal.supplier_terms || econ.supplier_terms || {},
          jurisdiction: deal.jurisdiction || "FEDERAL",
        }),
      });
      const c = res.card || {};
      const layers = (c.suggested_stack || []).map((L) =>
        `<li>${esc(L.label || L.role)}: $${esc(L.amount)}</li>`
      ).join("");
      const conf = c.capital_confirmation;
      let confHtml = "";
      if (conf) {
        confHtml = `<div class="still-needed" style="margin-top:.75rem">
          <strong>${esc(conf.title)}</strong>
          <p>Recorded deployable: $${esc(conf.recorded_deployable_capital)} · Deal needs: $${esc(conf.deal_requires_company_capital)}</p>
          <label>Actual available balance today <input id="fin-bal" type="number" step="0.01" /></label>
          <button type="button" class="btn primary" id="fin-confirm-cap">${esc(conf.action_label)}</button>
        </div>`;
      }
      el.innerHTML = `<h2>Financing</h2>
        <p><strong>Status: ${esc(c.plain_status || c.status || "—")}</strong></p>
        <p class="muted">${esc(c.status_means || "")}</p>
        <table class="compare-table" style="margin:.5rem 0">
          <tbody>
            <tr><td>Product cost</td><td>$${esc(c.product_cost || "—")}</td></tr>
            <tr><td>Freight</td><td>$${esc(c.freight || "0")}</td></tr>
            <tr><td>Total prepayment needed</td><td>$${esc(c.total_prepayment_need)}</td></tr>
            <tr><td>PO financing</td><td>$${esc(c.po_financing || "0")}</td></tr>
            <tr><td>Supplier terms</td><td>$${esc(c.supplier_terms_amount || "0")}</td></tr>
            <tr><td>Confirmed company capital</td><td>$${esc(c.confirmed_company_capital || c.company_capital_confirmed || "0")}</td></tr>
            <tr><td><strong>Owner/company cash still required</strong></td><td><strong>$${esc(c.owner_cash_required || "0")}</strong></td></tr>
            <tr><td>Estimated financing fees</td><td>$${esc(c.estimated_financing_cost || "0")}${c.financing_cost_unknown ? " (partially unknown)" : ""}</td></tr>
            <tr><td>Expected financed days</td><td>${esc(c.expected_financed_days != null ? c.expected_financed_days : "—")}</td></tr>
            <tr><td>Profit before financing</td><td>$${esc(c.profit_before_financing || "—")}</td></tr>
            <tr><td><strong>Profit after financing</strong></td><td><strong>$${esc(c.profit_after_financing || "—")}</strong></td></tr>
            <tr><td>Timing risk</td><td>${esc(c.timing_risk ? "YES" : (c.timing && c.timing.missing_dates && c.timing.missing_dates.length ? "UNKNOWN (missing dates)" : "NO"))}</td></tr>
          </tbody>
        </table>
        <details class="advanced"><summary>Stack detail</summary><ul>${layers || "<li>None yet</li>"}</ul></details>
        <p class="why"><strong>Next:</strong> ${esc(c.next_action || "—")}</p>
        ${c.lender_approval_still_required ? `<p class="muted">Lender approval still required for this specific deal.</p>` : ""}
        ${c.historical_patterns && c.historical_patterns.summary ? `<p class="muted">${esc(c.historical_patterns.summary)}</p>` : ""}
        ${confHtml}
        <button type="button" class="btn ghost" data-go-fin style="margin-top:.5rem">Financing Intelligence</button>`;
      el.querySelector("[data-go-fin]")?.addEventListener("click", () => navigate("financing"));
      const btn = document.getElementById("fin-confirm-cap");
      if (btn && conf) {
        btn.onclick = async () => {
          const amount = conf.deal_requires_company_capital;
          const prop = await api("/api/financing/opportunities/" + encodeURIComponent(deal.deal_id) + "/capital-reserve", {
            method: "POST",
            body: JSON.stringify({ amount }),
          });
          const rid = (prop.reservation || {}).reservation_id;
          const bal = document.getElementById("fin-bal")?.value;
          await api("/api/financing/capital-reservations/" + encodeURIComponent(rid) + "/confirm", {
            method: "POST",
            body: JSON.stringify({
              confirmed_by: "owner",
              actual_available_balance_today: bal || undefined,
            }),
          });
          toast("Capital commitment confirmed");
          fillFinancingCard(deal);
        };
      }
    } catch (e) {
      el.innerHTML = `<h2>Financing</h2><p class="muted">${esc(e.message || "Could not assess financing")}</p>
        <button type="button" class="btn ghost" data-go-fin>Open Financing Intelligence</button>`;
      el.querySelector("[data-go-fin]")?.addEventListener("click", () => navigate("financing"));
    }
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
    let html = `<h1>Quotes</h1><p class="lead">Simple quote queue.</p>
      <p><button type="button" class="btn secondary" data-go-channel>Open Owner Channel Tests</button></p>`;
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
    main.querySelector("[data-go-channel]")?.addEventListener("click", () => navigate("channel-tests"));
    bindDealActions();
  }

  async function renderLargeTest() {
    setHint("Large production test funnel — evidence from stores only; no auto outreach.");
    const [ui, progress, report] = await Promise.all([
      api("/api/m3/large-production-test/ui").catch(() => ({ status: "NO_UI" })),
      api("/api/m3/large-production-test/progress").catch(() => ({ progress_pct: 0 })),
      api("/api/m3/large-production-test/report").catch(() => ({ status: "NO_REPORT" })),
    ]);
    const fn = ui.funnel || report.funnel || {};
    let html = `<h1>Large Production Test</h1>
      <p class="lead">500-opportunity canonical funnel measurement. REAL_SUPPLIER_LOOP_PROVEN remains NO.</p>
      <p class="muted">Build ${esc(ui.build_version || BUILD)} · progress ${esc(progress.progress_pct || 0)}% · ${esc(progress.stage || "")}</p>
      <div class="meta-row">
        <span>Sample: <strong>${esc(ui.sample_count || report.actual_unique_sample || "—")}</strong></span>
        <span>LARGE_TEST_PASS: <strong>${esc(ui.LARGE_TEST_PASS || report.LARGE_TEST_PASS || "—")}</strong></span>
        <span>NEXT: ${esc(ui.NEXT_RUN_ALLOWED || report.NEXT_RUN_ALLOWED || "—")}</span>
      </div>
      <div class="meta-row">
        <span>Package verified: ${esc(fn.package_verified)}</span>
        <span>Identity: ${esc(fn.identity_ready)}</span>
        <span>Revenue: ${esc(fn.revenue_ready)}</span>
        <span>Quote required: ${esc(fn.quote_required)}</span>
        <span>Economics: ${esc(fn.economics_ready)}</span>
        <span>Bid ready: ${esc(fn.bid_ready)}</span>
      </div>
      <h2>Top readiness</h2>`;
    const top = ui.top_readiness || [];
    if (!top.length) html += emptyBox("No large-test results yet.");
    for (const t of top) {
      html += `<article class="panel" style="margin-top:.5rem">
        <div class="deal-card-top"><h3>${esc(t.opportunity)}</h3>${badge(t.economics || "—", t.economics === "READY" ? "green" : "yellow")}</div>
        <div class="meta-row">
          <span>Buyer: ${esc(t.buyer)}</span>
          <span>Identity: ${esc(t.identity)}</span>
          <span>Revenue: ${esc(t.revenue)}</span>
          <span>Acquisition: ${esc(t.acquisition)}</span>
          <span>Basket: ${esc(t.basket)}</span>
        </div>
        <p class="muted"><strong>Next:</strong> ${esc(t.next_action)}</p>
      </article>`;
    }
    html += `<h2>Quote reserve</h2>`;
    for (const q of (ui.top_quote_reserve || ui.quote_reserve || []).slice(0, 10)) {
      html += `<article class="panel" style="margin-top:.5rem">
        <h3>${esc(q.opportunity || q.opportunity_id)}</h3>
        <p class="muted">${esc(q.next_action || "")}</p>
      </article>`;
    }
    html += `<h2>Blocked</h2>`;
    for (const b of (ui.blocked || []).slice(0, 10)) {
      html += `<article class="panel" style="margin-top:.5rem">
        <h3>${esc(b.opportunity_id)}</h3>
        <p class="muted">${esc(b.reason)} — ${esc(b.next_action || "")}</p>
      </article>`;
    }
    main.innerHTML = html;
  }

  function packageStateLabel(state) {
    const s = String(state || "");
    if (s.startsWith("PACKAGE_ACQUIRED")) return "PACKAGE READY";
    if (s === "PACKAGE_RETRYABLE") return "PACKAGE RETRYABLE";
    if (s === "PACKAGE_LOCKED_AGGREGATOR") return "LOCKED AGGREGATOR — SEARCHING OFFICIAL SOURCE";
    if (s === "PACKAGE_OFFICIAL_SOURCE_NOT_FOUND" || s === "PACKAGE_NOT_FOUND_FREE") return "OFFICIAL SOURCE NOT FOUND";
    if (s === "PACKAGE_DEFERRED_SAM_PRIORITY") return "SAM DEFERRED — PRIORITY";
    if (s === "PACKAGE_API_CREDIT_EXHAUSTED" || s === "PACKAGE_NOT_ATTEMPTED_SAM_BUDGET") return "SAM DEFERRED — DAILY BUDGET";
    if (s === "PACKAGE_API_CREDIT_REQUIRED") return "SAM CREDIT QUEUED";
    if (s.includes("SAM") || s.includes("RECOVER")) return "RECOVERING FREE PACKAGE";
    return s || "PACKAGE MISSING";
  }

  async function renderPackageRecovery() {
    setHint("Package recovery on frozen LARGE_TEST_CORPUS_V1 — SAM max 10/day, reserve 1.");
    const [ui, progress, report] = await Promise.all([
      api("/api/m3/package-recovery/ui").catch(() => ({ status: "NO_UI" })),
      api("/api/m3/package-recovery/progress").catch(() => ({ progress_pct: 0 })),
      api("/api/m3/package-recovery/report").catch(() => ({ status: "NO_REPORT" })),
    ]);
    const meter = ui.sam_meter || (report.sam || {}).budget || {};
    const counts = ui.package_counts || report.package_states || {};
    let html = `<h1>Package Recovery</h1>
      <p class="lead">Free BidNet/official alternate recovery + SAM credit-aware queue. Same 500 corpus.</p>
      <p class="muted">Build ${esc(ui.build_version || BUILD)} · progress ${esc(progress.progress_pct || 0)}% · ${esc(progress.stage || "")}</p>
      <div class="meta-row">
        <span>Before: <strong>${esc(ui.before_packages ?? report.before_packages ?? "—")}</strong></span>
        <span>After: <strong>${esc(ui.after_packages ?? report.after_packages ?? "—")}</strong></span>
        <span>Net new: <strong>${esc(ui.newly_recovered ?? report.net_new ?? "—")}</strong></span>
        <span>PASS: <strong>${esc(ui.PACKAGE_RECOVERY_PASS || report.PACKAGE_RECOVERY_PASS || "—")}</strong></span>
        <span>NEXT: ${esc(ui.NEXT_RUN_ALLOWED || report.NEXT_RUN_ALLOWED || "—")}</span>
      </div>
      <h2>SAM credit meter</h2>
      <div class="meta-row">
        <span>Calls used today: <strong>${esc(meter.calls_used ?? "—")} / ${esc(meter.daily_max ?? 10)}</strong></span>
        <span>Remaining: <strong>${esc(meter.calls_remaining ?? "—")}</strong></span>
        <span>Reserved: <strong>${esc(meter.calls_reserved ?? 1)}</strong></span>
        <span>Dup prevented: ${esc(meter.duplicate_calls_prevented ?? 0)}</span>
      </div>
      <h2>Package states</h2>
      <div class="meta-row">`;
    const order = [
      "PACKAGE_ACQUIRED", "PACKAGE_ACQUIRED_CACHED", "PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE",
      "PACKAGE_LOCKED_AGGREGATOR", "PACKAGE_NOT_FOUND_FREE", "PACKAGE_API_CREDIT_REQUIRED",
      "PACKAGE_API_CREDIT_EXHAUSTED", "PACKAGE_DEFERRED_SAM_PRIORITY", "PACKAGE_RETRYABLE",
    ];
    for (const k of order) {
      if (counts[k] != null) html += `<span>${esc(packageStateLabel(k))}: <strong>${esc(counts[k])}</strong></span>`;
    }
    html += `</div><h2>SAM priority queue</h2>`;
    const queue = ui.top_sam_queue || report.sam_priority_top20 || [];
    if (!queue.length) html += emptyBox("No SAM queue yet.");
    for (const q of queue.slice(0, 12)) {
      html += `<article class="panel" style="margin-top:.5rem">
        <div class="deal-card-top">
          <h3>${esc(q.opportunity || q.opportunity_id)}</h3>
          ${badge(packageStateLabel(q.status), q.selected ? "green" : "yellow")}
        </div>
        <div class="meta-row">
          <span>Score: <strong>${esc(q.priority_score)}</strong></span>
          <span>Cached: ${esc(q.cached)}</span>
          <span>Selected: ${esc(q.selected_today ?? q.selected)}</span>
          <span>${esc((q.reasons || q.reason || []).join?.(", ") || q.reason || "")}</span>
        </div>
        <button type="button" class="btn secondary" data-promote-sam="${esc(q.opportunity || q.opportunity_id)}">Promote (owner)</button>
      </article>`;
    }
    main.innerHTML = html;
    main.querySelectorAll("[data-promote-sam]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const oid = btn.getAttribute("data-promote-sam");
        const res = await api("/api/m3/package-recovery/promote-sam", {
          method: "POST",
          body: JSON.stringify({ opportunity_id: oid, owner_override: false }),
        }).catch((e) => ({ ok: false, error: String(e) }));
        toast(res.ok ? `Promoted ${oid}` : `Promote failed: ${res.error || res.hint || "budget"}`);
        if (res.ok) renderPackageRecovery();
      });
    });
  }

  async function renderBidnetEngine() {
    setHint("BidNet baseline → twice-daily incremental production. Canary then unattended baseline.");
    const [report, progress, recovery, recoveryProgress, prodStatus, prodProgress] = await Promise.all([
      api("/api/m3/bidnet-engine/report").catch(() => ({ status: "NO_REPORT" })),
      api("/api/m3/bidnet-engine/progress").catch(() => ({ progress_pct: 0 })),
      api("/api/m3/bidnet-engine/recovery/report").catch(() => ({ status: "NO_REPORT" })),
      api("/api/m3/bidnet-engine/recovery/progress").catch(() => ({ progress_pct: 0 })),
      api("/api/m3/bidnet-production/status").catch(() => ({ status: "NO_STATUS" })),
      api("/api/m3/bidnet-production/progress").catch(() => ({ progress_pct: 0 })),
    ]);
    const b = report.baseline || {};
    const after = report.after || {};
    const q = report.queue || {};
    const bl = report.backlog || {};
    const rec = recovery.recovery || {};
    const stab = recovery.stability || {};
    const fin = recovery.final || {};
    const thr = recovery.thread_control || {};
    const conc = (report.concurrency || [])
      .map(
        (s) =>
          `<tr><td>${esc(s.logical_workers ?? s.workers)}</td><td>${esc(s.browser_workers ?? "—")}</td><td>${esc(
            s.throughput_per_min
          )}</td><td>${esc(s.errors)}</td><td>${esc(s.auth_failures)}</td></tr>`
      )
      .join("");
    const canary = prodStatus.canary || {};
    main.innerHTML = `<h1>BidNet Engine</h1>
      <p class="lead">One-time baseline (5L/2B) then permanent 06:10/14:10 incremental sync. Discovery frozen.</p>
      <p class="muted">Build ${esc(prodStatus.build || report.build || recovery.build || "")} · production ${esc(
        prodProgress.stage || prodStatus.phase || "idle"
      )} ${esc(prodProgress.progress_pct || prodStatus.percent || 0)}% · recovery ${esc(
        recoveryProgress.stage || "idle"
      )} ${esc(recoveryProgress.progress_pct || 0)}% · Pass ${esc(
        recovery.BIDNET_ENGINE_RECOVERY_PASS || report.BIDNET_INCREMENTAL_PARALLEL_PASS || report.PASS_FAIL || "—"
      )}</p>
      <div class="meta-row">
        <span>Valid open: <strong>21,976</strong></span>
        <span>Product/Mixed: ${esc(b.product_mixed_total ?? 13270)}</span>
        <span>Completed: ${esc(prodStatus.completed ?? rec.durably_saved ?? b.cumulative_complete ?? "—")}</span>
        <span>Remaining: ${esc(prodStatus.remaining ?? rec.remaining_baseline ?? b.remaining ?? "—")}</span>
        <span>Logical: ${esc(prodStatus.logical_workers ?? (stab.selected || {}).logical ?? 5)}</span>
        <span>Browsers: ${esc(prodStatus.browser_workers ?? (stab.selected || {}).browsers ?? 2)}</span>
        <span>5m/15m/60m: ${esc(prodStatus.throughput_5m ?? "—")}/${esc(prodStatus.throughput_15m ?? "—")}/${esc(
          prodStatus.throughput_60m ?? "—"
        )}</span>
        <span>ETA h: ${esc(prodStatus.eta_hours ?? "—")}</span>
      </div>
      <div class="meta-row">
        <span>Canary 15/30/60: ${esc((canary["15"] || {}).PASS_FAIL || "—")}/${esc(
          (canary["30"] || {}).PASS_FAIL || "—"
        )}/${esc((canary["60"] || {}).PASS_FAIL || "—")}</span>
        <span>API: ${esc(prodStatus.api_health ?? "—")}</span>
        <span>502: ${esc(prodStatus.http_502 ?? 0)}</span>
        <span>Thread fails: ${esc(prodStatus.thread_failures ?? 0)}</span>
        <span>Checkpoint age s: ${esc(prodStatus.checkpoint_age_s ?? "—")}</span>
        <span>OPENBLAS: ${esc(thr.OPENBLAS_NUM_THREADS ?? "1")}</span>
        <span>Next: ${esc(fin.NEXT_RUN_ALLOWED || report.NEXT_RUN_ALLOWED || "CONTINUE_BASELINE")}</span>
        <span>Queued: ${esc(q.total_queued ?? "—")}</span>
      </div>
      <h2>Concurrency test</h2>
      <table class="data"><thead><tr><th>Logical</th><th>Browsers</th><th>Throughput/min</th><th>Errors</th><th>Auth failures</th></tr></thead><tbody>${
        conc || "<tr><td colspan='5'>No concurrency results yet.</td></tr>"
      }</tbody></table>`;
  }

  async function renderBidnetDownstream() {
    setHint("BidNet downstream census of the frozen 21,976 valid-open corpus. Discovery is not rerun.");
    const [report, progress] = await Promise.all([
      api("/api/m3/bidnet-downstream/report").catch(() => ({ status: "NO_REPORT" })),
      api("/api/m3/bidnet-downstream/progress").catch(() => ({ progress_pct: 0 })),
    ]);
    const cls = report.classification || {};
    const pipe = report.product_pipeline || {};
    const pkg = report.package || {};
    const elig = report.eligibility || {};
    const econ = report.economics || {};
    const acq = report.acquisition || {};
    const basket = report.basket || {};
    const deep = report.deep || {};
    const ready =
      Number(pkg.PACKAGE_ACQUIRED_BIDNET || 0) + Number(pkg.PACKAGE_ACQUIRED_OFFICIAL_SOURCE || 0);
    const blockers = (report.bottlenecks || [])
      .map(
        (b) =>
          `<tr><td>${esc(b.reason)}</td><td>${esc(b.count)}</td><td>${esc(b.percent)}%</td><td>${esc(
            b.recommended_action
          )}</td></tr>`
      )
      .join("");
    const top = (report.top_25_readiness || [])
      .map(
        (r) =>
          `<tr><td>${esc(r.buyer)}</td><td>${esc(r.title)}</td><td>${esc(r.category)}</td><td>${esc(
            r.package
          )}</td><td>${esc(r.next_action)}</td></tr>`
      )
      .join("");
    main.innerHTML = `<h1>BidNet Downstream</h1>
      <p class="lead">Production census of the frozen valid-open BidNet corpus. Missing package access is not a product rejection.</p>
      <p class="muted">Build ${esc(report.build || progress.build_version || "")} · ${esc(
        progress.stage || progress.phase || "idle"
      )} · ${esc(progress.progress_pct || 0)}%</p>
      <div class="meta-row">
        <span>Valid open: <strong>${esc(report.input_valid_open ?? "21,976")}</strong></span>
        <span>Distinct: ${esc(report.distinct_opportunities ?? "—")}</span>
        <span>Collapsed duplicates: ${esc(report.collapsed_duplicate_list_identities ?? "—")}</span>
        <span>Pass: <strong>${esc(report.BIDNET_DOWNSTREAM_PASS || report.PASS_FAIL || "—")}</strong></span>
        <span>Next: ${esc(report.NEXT_RUN_ALLOWED || "—")}</span>
        <span>Deep done: ${esc(deep.deep_complete_total ?? "—")}</span>
        <span>Deep pending: ${esc(report.pending_deep_remaining ?? deep.pending_remaining ?? "—")}</span>
      </div>
      <h2>Classification</h2>
      <div class="meta-row">
        <span>Classified: ${esc(report.classification_accounted ?? "—")}</span>
        <span>Product: ${esc(cls.PRODUCT ?? "—")}</span>
        <span>Mixed: ${esc(cls.MIXED_PRODUCT_MATERIAL ?? "—")}</span>
        <span>Service: ${esc(cls.SERVICE ?? "—")}</span>
        <span>Construction: ${esc(cls.CONSTRUCTION ?? "—")}</span>
        <span>Unknown: ${esc(cls.UNKNOWN ?? "—")}</span>
      </div>
      <h2>Downstream funnel</h2>
      <div class="meta-row">
        <span>Detail complete: ${esc(pipe.DETAIL_COMPLETE ?? "—")}</span>
        <span>Package ready: ${esc(ready)}</span>
        <span>Eligibility clear: ${esc(elig.ELIGIBILITY_CLEAR ?? "—")}</span>
        <span>Lines extracted: 0</span>
        <span>Identity ready: 0</span>
        <span>Revenue ready: 0</span>
        <span>Public acquisition ready: ${esc(acq.public_price_ready ?? 0)}</span>
        <span>Quote required: ${esc(acq.quote_required_opportunities ?? 0)}</span>
        <span>Basket ready: ${esc(basket.ready ?? 0)}</span>
        <span>Economics ready: ${esc(econ.ready ?? 0)}</span>
      </div>
      <h2>Bottlenecks</h2>
      <table class="data"><thead><tr><th>Reason</th><th>Count</th><th>Percent</th><th>Action</th></tr></thead><tbody>${
        blockers || "<tr><td colspan='4'>No census yet.</td></tr>"
      }</tbody></table>
      <h2>Top opportunities</h2>
      <table class="data"><thead><tr><th>Buyer</th><th>Title</th><th>Category</th><th>Package</th><th>Next action</th></tr></thead><tbody>${
        top || "<tr><td colspan='5'>No ranked product opportunities yet.</td></tr>"
      }</tbody></table>`;
  }

  async function renderBidnetProduction() {
    setHint("BidNet authenticated ingestion — session, 192 acceptance, full discovery health.");
    const [ui, progress, report] = await Promise.all([
      api("/api/m3/bidnet-full-production/ui").catch(() => ({ status: "NO_UI" })),
      api("/api/m3/bidnet-full-production/progress").catch(() => ({ progress_pct: 0 })),
      api("/api/m3/bidnet-full-production/report").catch(() => ({ status: "NO_REPORT" })),
    ]);
    const sess = ui.session || report.session || {};
    const disc = ui.discovery || report.discovery || {};
    const acc = ui.acceptance_192 || report.acceptance_192 || {};
    const auth = ui.bidnet_auth || {};
    let html = `<h1>BidNet Production</h1>
      <p class="lead">Full authenticated detail + explicit package states on frozen 192 BidNet corpus.</p>
      <p class="muted">Build ${esc(ui.build_version || BUILD)} · progress ${esc(progress.progress_pct || 0)}%</p>
      <div class="meta-row">
        <span>Session: <strong>${esc(sess.authenticated ? "VALID" : "INVALID")}</strong></span>
        <span>Reused: ${esc(sess.session_reused)}</span>
        <span>Auth status: ${esc(auth.status || auth.connection_status || "—")}</span>
      </div>
      <h2>Discovery</h2>
      <div class="meta-row">
        <span>Reported open: ${esc(disc.reported_open ?? "—")}</span>
        <span>Retrieved unique: ${esc(disc.unique ?? "—")}</span>
        <span>Complete: ${esc(disc.complete ?? "—")}</span>
        <span>Truncated: ${esc(disc.truncated ?? "—")}</span>
      </div>
      <h2>192 acceptance</h2>
      <div class="meta-row">
        <span>Buyer resolved: ${esc(acc.buyer_resolved)} / ${esc(acc.sample)}</span>
        <span>Solicitation: ${esc(acc.solicitation_resolved)}</span>
        <span>Detail: ${esc(acc.detail_acquired)}</span>
        <span>Packages: ${esc(acc.package_acquired)}</span>
        <span>States explained: ${esc(acc.package_explained)}</span>
        <span>PASS: <strong>${esc(acc.PASS_FAIL || report.BIDNET_PRODUCTION_PASS || "—")}</strong></span>
      </div>
      <h2>Package access</h2>
      <div class="meta-row">`;
    const pa = ui.package_access || report.package_access || {};
    for (const [k, v] of Object.entries(pa).slice(0, 12)) {
      html += `<span>${esc(k)}: <strong>${esc(v)}</strong></span>`;
    }
    html += `</div>`;
    main.innerHTML = html;
  }

  async function renderChannelTests() {
    setHint("Copy request → send externally yourself → Mark Sent. Never auto-sends.");
    const data = await api("/api/m3/owner-channel/cards");
    const cards = data.cards || [];
    let html = `<h1>Owner Channel Tests</h1>
      <p class="lead">Four production-ready packets. <strong>You</strong> send externally — M3 never auto-sends.</p>
      <p class="muted">Build ${esc(data.build_version || BUILD)} · do_not_send_automatically=true</p>`;
    if (!cards.length) {
      html += emptyBox("No channel test cards yet. Run owner-channel-tests build.");
      main.innerHTML = html;
      return;
    }
    for (const c of cards) {
      const sent = c.sent_state;
      const obs = c.observability || {};
      html += `
        <article class="panel" style="margin-top:.75rem" data-packet="${esc(c.packet_id)}">
          <div class="deal-card-top">
            <h2>${esc(c.Supplier)}</h2>
            ${badge(c.Status || "READY", c.Status === "READY" ? "green" : "red")}
            ${sent ? badge("SENT", "blue") : badge("READY TO SEND", "yellow")}
          </div>
          <div class="meta-row">
            <span>Opportunity: ${esc(c.Opportunity)}</span>
            <span>Lines: <strong>${esc(c.Lines)}</strong></span>
            <span>Trace: ${esc(c.Packet_trace)}</span>
            <span>Qty diff: ${esc(c.Quantity_diff)}</span>
            <span>Route: ${esc(c.Contact_route_class)}</span>
          </div>
          <div class="meta-row" style="margin-top:.35rem">
            <span>Packet: ${esc(obs.packet_status || "—")}</span>
            <span>Sent: ${esc(obs.sent_date || "not sent")}</span>
            <span>Response: ${esc(obs.supplier_response || "—")}</span>
            <span>Quote: ${esc(obs.quote_status || "—")}</span>
            <span>Matched lines: ${esc(obs.line_match_count != null ? obs.line_match_count : "—")}</span>
            <span>Rejected lines: ${esc(obs.rejected_line_count != null ? obs.rejected_line_count : "—")}</span>
          </div>
          <div class="meta-row">
            <span>Basket: ${esc(obs.basket_before || "—")} → ${esc(obs.basket_after || "—")}</span>
            <span>Economics: ${esc(obs.economics_before || "—")} → ${esc(obs.economics_after || "—")}</span>
          </div>
          <p class="muted"><strong>Next:</strong> ${esc(obs.current_next_action || "Copy request → send externally → Mark Sent")}</p>
          ${obs.plain_english ? `<p class="muted">${esc(obs.plain_english)}</p>` : ""}
          <p class="muted">Contact: ${c.Contact_route ? `<a href="${esc(c.Contact_route)}" target="_blank" rel="noopener">${esc(c.Contact_route)}</a>` : "—"}
            ${c.phone ? ` · Phone: ${esc(c.phone)}` : ""}</p>
          <p class="muted">${esc(c.account_requirement || "")}</p>
          <p><strong>${esc(c.subject || "")}</strong></p>
          <pre class="muted" style="white-space:pre-wrap;max-height:12rem;overflow:auto">${esc(c.request_body || "")}</pre>
          <div class="meta-row" style="margin-top:.5rem;gap:.35rem;flex-wrap:wrap">
            <button type="button" class="btn primary" data-copy>COPY REQUEST</button>
            <a class="btn secondary" href="/api/m3/owner-channel/packets" target="_blank" rel="noopener">EXPORT PACKET</a>
            <button type="button" class="btn secondary" data-mark-sent>MARK SENT</button>
            <button type="button" class="btn ghost" data-record>RECORD RESPONSE</button>
            <button type="button" class="btn ghost" data-upload>UPLOAD QUOTE</button>
            <button type="button" class="btn ghost" data-audit>AUDIT TRAIL</button>
            ${obs.can_revert && obs.active_quote_id ? `<button type="button" class="btn ghost" data-revert data-qid="${esc(obs.active_quote_id)}">REVERT QUOTE</button>` : ""}
          </div>
          <p class="muted" data-feedback style="margin-top:.5rem"></p>
        </article>`;
    }
    main.innerHTML = html;
    main.querySelectorAll("[data-packet]").forEach((card) => {
      const pid = card.getAttribute("data-packet");
      const c = cards.find((x) => x.packet_id === pid) || {};
      card.querySelector("[data-copy]")?.addEventListener("click", async () => {
        const text = `${c.subject || ""}\n\n${c.request_body || ""}`;
        try {
          await navigator.clipboard.writeText(text);
          toast("Request copied — send from your own email/browser.");
        } catch (_) {
          toast("Copy failed — select text manually.");
        }
      });
      card.querySelector("[data-mark-sent]")?.addEventListener("click", async () => {
        const method = window.prompt("How did you send? (email / web_form / phone)", "web_form") || "MANUAL_EXTERNAL";
        const contact = window.prompt("Contact used (URL/phone/email)", c.Contact_route || "") || "";
        try {
          await api("/api/m3/owner-channel/mark-sent", {
            method: "POST",
            body: JSON.stringify({
              packet_id: pid,
              supplier: c.Supplier,
              method,
              contact_used: contact,
              owner_notes: "Marked sent by owner from Channel Tests UI",
            }),
          });
          toast("Marked SENT → WAITING_FOR_SUPPLIER_QUOTE");
          renderChannelTests();
        } catch (e) {
          toast(e.message);
        }
      });
      card.querySelector("[data-record]")?.addEventListener("click", async () => {
        const outcome = window.prompt(
          "Outcome: QUOTE_RECEIVED | PARTIAL_QUOTE_RECEIVED | DECLINED_TO_QUOTE | NO_RESPONSE | ACCOUNT_REQUIRED | REFERRED_TO_DISTRIBUTOR | OTHER",
          "NO_RESPONSE"
        );
        if (!outcome) return;
        try {
          await api("/api/m3/owner-channel/record-response", {
            method: "POST",
            body: JSON.stringify({ packet_id: pid, outcome }),
          });
          toast("Response recorded: " + outcome);
          renderChannelTests();
        } catch (e) {
          toast(e.message);
        }
      });
      card.querySelector("[data-upload]")?.addEventListener("click", async () => {
        const mpn = window.prompt("Real quote MPN/model (must match packet line)");
        if (!mpn) return;
        const unit = window.prompt("Unit price");
        const qnum = window.prompt("Quote number/reference");
        const artifact = window.prompt("Source artifact filename/path", "owner_uploaded_quote.pdf");
        try {
          const res = await api("/api/m3/owner-channel/ingest-quote", {
            method: "POST",
            body: JSON.stringify({
              packet_id: pid,
              quote: {
                origin: "REAL_SUPPLIER_QUOTE",
                supplier: c.Supplier,
                quote_date: new Date().toISOString().slice(0, 10),
                quote_number_or_reference: qnum,
                source_artifact: artifact,
                freight_treatment: "TBD",
                validity: "30 days",
                lines: [
                  {
                    exact_mpn_model: mpn,
                    mpn,
                    qty: 1,
                    uom: "EA",
                    unit_price: Number(unit),
                    extended_price: Number(unit),
                  },
                ],
              },
            }),
          });
          const fb = card.querySelector("[data-feedback]");
          if (fb) fb.textContent = res.ok
            ? `Ingested · basket=${res.result?.basket_state} · economics=${res.result?.economics_state} · profit_created=${res.result?.profit_created} · quote_id=${res.result?.observability?.quote_id || "—"}`
            : `Rejected: ${res.result?.blocked_reason || res.error}`;
          toast(res.ok ? "Real quote ingested" : "Quote rejected");
          if (res.ok) renderChannelTests();
        } catch (e) {
          toast(e.message);
        }
      });
      card.querySelector("[data-audit]")?.addEventListener("click", async () => {
        try {
          const trail = await api("/api/m3/quote-observability/" + encodeURIComponent(pid));
          const events = (trail.events || []).slice(-8).map((e) =>
            `${e.timestamp || ""} · ${e.event_type || ""} · ${e.reason || ""}`
          ).join("\n") || "(no events yet)";
          const snap = (trail.snapshots || []).slice(-1)[0];
          const delta = snap
            ? `\n\nLast delta (${snap.DELTA?.changed_count || 0} fields):\n${JSON.stringify(snap.DELTA?.changed_fields || {}, null, 2)}`
            : "\n\nNo before/after snapshot yet.";
          window.alert(`Audit trail for ${pid}\nEvents: ${trail.events?.length || 0}\n\n${events}${delta}`);
        } catch (e) {
          toast(e.message);
        }
      });
      card.querySelector("[data-revert]")?.addEventListener("click", async () => {
        const qid = card.querySelector("[data-revert]")?.getAttribute("data-qid");
        if (!qid) return;
        if (!window.confirm("Deactivate/revert quote " + qid + "? Underlying failed states stay recorded.")) return;
        try {
          const res = await api("/api/m3/quote-observability/deactivate", {
            method: "POST",
            body: JSON.stringify({ quote_id: qid, reason: "owner_ui_revert" }),
          });
          toast(res.ok ? "Quote deactivated" : (res.error || "Revert failed"));
          renderChannelTests();
        } catch (e) {
          toast(e.message);
        }
      });
    });
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
    const missing = data.data_status === "DATA_SOURCE_MISSING";
    main.innerHTML = `
      <h1>Registrations</h1>
      <p class="lead">Why spend time? Each card shows what you unlock from unique opportunity IDs.</p>
      ${missing ? `<div class="still-needed"><strong>Opportunity store missing</strong><p>Unlock counts are not fabricated.</p></div>` : ""}
      <p class="muted">Unique blocked opportunities across unlocks: <strong>${esc(data.unique_blocked_opportunities ?? "—")}</strong></p>
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
            <span>${esc(r.opportunity_count ?? r.opportunities_unlocked)} current relevant opportunities</span>
          </div>
          <p class="why">${esc(r.why)}</p>
          <div class="deal-actions">
            <button type="button" class="btn primary" data-reg="${esc(r.portal_id)}">REGISTER NOW</button>
            <button type="button" class="btn ghost" data-view-opps="${esc(r.portal_id)}">View Opportunities</button>
          </div>
        </article>`).join("") : emptyBox(data.empty)}`;
    main.querySelectorAll("[data-reg]").forEach((b) => {
      b.onclick = () => navigate("registration", { id: b.getAttribute("data-reg") });
    });
    main.querySelectorAll("[data-view-opps]").forEach((b) => {
      b.onclick = () => navigate("registration-opps", { id: b.getAttribute("data-view-opps") });
    });
  }

  async function renderRegistrationOpps(params) {
    const data = await api("/api/ui/registrations/" + encodeURIComponent(params.id) + "/opportunities");
    if (data.data_status === "DATA_SOURCE_MISSING" || data.error === "OPPORTUNITY_STORE_MISSING") {
      main.innerHTML = `<p><button type="button" class="btn ghost" data-back>← Back</button></p>
        <div class="still-needed"><strong>Opportunity store missing</strong><p>${esc(data.message || "")}</p></div>`;
      main.querySelector("[data-back]").onclick = () => navigate("registrations");
      return;
    }
    main.innerHTML = `
      <p><button type="button" class="btn ghost" data-back>← Back</button></p>
      <h1>${esc(data.portal || "Unlock")} — Opportunities</h1>
      <p class="lead">Exact unique IDs behind this unlock: <strong>${esc(data.opportunity_count)}</strong></p>
      <p class="muted">${esc((data.opportunity_ids || []).join(", ") || "—")}</p>
      ${(data.items || []).length ? data.items.map((d) => dealCard(d)).join("") : emptyBox("No linked opportunities found for this unlock.")}`;
    main.querySelector("[data-back]").onclick = () => navigate("registrations");
    bindDealActions();
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
              ${d.response_project_id && d.r5 && d.r5.approval && d.r5.approval.status === "APPROVED" ? `<button type="button" class="btn ghost" data-freeze="${esc(d.response_project_id)}">FREEZE APPROVED PACKAGE</button>` : ""}
              ${d.response_project_id && d.r5 && d.r5.approval && d.r5.approval.status === "APPROVED" ? `<button type="button" class="btn primary" data-dry-run-submit="${esc(d.response_project_id)}">GUIDED DRY-RUN SUBMIT</button>` : ""}
              ${d.response_project_id && d.r5 && d.r5.approval && d.r5.approval.status === "APPROVED" ? `<button type="button" class="btn ghost" data-dry-receipt="${esc(d.response_project_id)}">RECORD DRY-RUN RECEIPT</button>` : ""}
            </div>
            <p class="muted">Guided dry-run only · never contacts portals · rebuild after approve invalidates approval</p>
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
    main.querySelectorAll("[data-freeze]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-freeze");
        try {
          toast("Freezing approved package…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/freeze-submission", {
            method: "POST",
            body: JSON.stringify({}),
          });
          toast(res.ok ? "Package frozen (not submitted)" : (res.error || "Freeze failed"));
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Freeze failed");
        }
      };
    });
    main.querySelectorAll("[data-dry-run-submit]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-dry-run-submit");
        try {
          toast("Running guided dry-run submission (no portal contact)…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/submission-events", {
            method: "POST",
            body: JSON.stringify({ dry_run: true, submitted_by: "operator" }),
          });
          toast(res.ok ? "Dry-run recorded · TEST SUBMISSION — NOT SENT" : (res.error || "Dry-run failed"));
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Dry-run failed");
        }
      };
    });
    main.querySelectorAll("[data-dry-receipt]").forEach((b) => {
      b.onclick = async () => {
        const rid = b.getAttribute("data-dry-receipt");
        try {
          toast("Capturing dry-run receipt…");
          const res = await api("/api/response-projects/" + encodeURIComponent(rid) + "/receipt", {
            method: "POST",
            body: JSON.stringify({ dry_run: true, confirmation_number: "DRY-UI-" + Date.now(), source: "operator_ui" }),
          });
          toast(res.ok ? "Dry-run receipt captured" : (res.error || "Receipt failed"));
          renderBidPrep();
        } catch (e) {
          toast(e.message || "Receipt failed");
        }
      };
    });
  }

  async function renderFinancing(params) {
    const tab = params.tab || "sources";
    const data = await api("/api/ui/financing?tab=" + encodeURIComponent(tab));
    const d = data.dashboard || {};
    const cap = d.capital || {};
    const tabs = (data.tabs || []).map((t) =>
      `<button type="button" class="btn ${t.id === tab ? "primary" : "ghost"}" data-ftab="${esc(t.id)}">${esc(t.label)}</button>`
    ).join(" ");
    let body = "";
    if (tab === "sources") {
      const rows = (data.sources || []).map((s) =>
        `<tr><td>${esc(s.company_name)}</td><td>${esc(s.source_type)}</td><td>${esc(s.preference)}</td><td>${s.active ? "Active" : "Inactive"}</td><td>${esc(s.last_verified_date || "—")}</td></tr>`
      ).join("");
      body = `<section class="panel"><h2>Sources</h2>
        <table class="compare-table"><thead><tr><th>Name</th><th>Type</th><th>Preference</th><th>Status</th><th>Last verified</th></tr></thead>
        <tbody>${rows || "<tr><td colspan=5>No sources yet — add call notes or a lender.</td></tr>"}</tbody></table>
        <h3 style="margin-top:1rem">Add lender</h3>
        <label>Company <input id="fs-name" /></label>
        <label>Type <select id="fs-type"><option value="PO_FINANCE_COMPANY">PO finance</option><option value="FACTOR">Factor</option><option value="SUPPLIER_DISTRIBUTOR">Supplier</option><option value="BANK_CREDIT_FACILITY">Bank</option><option value="OTHER">Other</option></select></label>
        <label>Contact <input id="fs-contact" /></label>
        <label>Phone <input id="fs-phone" /></label>
        <button type="button" class="btn primary" id="fs-save">Add lender</button>
      </section>`;
    } else if (tab === "add") {
      body = `<section class="panel"><h2>Add Information</h2>
        <p class="muted">Paste messy call notes. Extracted facts stay proposed until you approve them.</p>
        <label>Company / source id <input id="fn-company" placeholder="TradeCap or source id" /></label>
        <label>Contact <input id="fn-contact" /></label>
        <label>Paste call notes<textarea id="fn-notes" rows="8" style="width:100%"></textarea></label>
        <button type="button" class="btn primary" id="fn-save">Extract proposed facts</button>
        <h3 style="margin-top:1rem">Awaiting review</h3>
        <ul id="fn-facts">${(data.proposed_facts || []).map((f) =>
          `<li data-fid="${esc(f.fact_id)}"><strong>${esc(f.field)}</strong> = ${esc(String(f.value))}
            <span class="muted">(${esc(f.confidence)}) “${esc(f.source_snippet || "")}”</span>
            <button type="button" class="btn ghost" data-fdec="APPROVE">Approve</button>
            <button type="button" class="btn ghost" data-fdec="REJECT">Reject</button>
          </li>`).join("") || "<li>No proposed facts</li>"}</ul>
      </section>`;
    } else if (tab === "capital") {
      body = `<section class="panel"><h2>Capital</h2>
        <p>Business cash: <strong>$${esc(cap.business_cash || "0")}</strong> · Deployable (potential only): <strong>$${esc(cap.deployable_capital || "0")}</strong></p>
        <p class="muted">${esc(cap.note || "Never auto-spent.")}</p>
        <label>Business cash <input id="fc-cash" value="${esc(cap.business_cash || "0")}" /></label>
        <label>Operating reserve <input id="fc-reserve" value="${esc(cap.minimum_operating_reserve || "0")}" /></label>
        <button type="button" class="btn primary" id="fc-save">Update capital</button>
        <h3 style="margin-top:1rem">Reservations</h3>
        <ul>${(data.reservations || []).slice(-15).reverse().map((r) =>
          `<li>${esc(r.opportunity_id)} · $${esc(r.amount)} · ${esc(r.status)}</li>`).join("") || "<li>None</li>"}</ul>
      </section>`;
    } else if (tab === "rules") {
      body = `<section class="panel"><h2>Rules / Evidence</h2>
        <p class="muted">Only approved facts affect opportunity financing decisions.</p>
        <h3>Approved</h3>
        <ul>${(data.approved_facts || []).map((f) =>
          `<li><strong>${esc(f.field)}</strong> = ${esc(String(f.value))} · ${esc(f.evidence_type || "")}</li>`).join("") || "<li>None</li>"}</ul>
        <h3>Proposed</h3>
        <ul>${(data.proposed_facts || []).map((f) =>
          `<li data-fid="${esc(f.fact_id)}"><strong>${esc(f.field)}</strong> = ${esc(String(f.value))}
            <button type="button" class="btn ghost" data-fdec="APPROVE">Approve</button>
            <button type="button" class="btn ghost" data-fdec="REJECT">Reject</button></li>`).join("") || "<li>None</li>"}</ul>
      </section>`;
    } else {
      body = `<section class="panel"><h2>Deal History</h2>
        <ul>${(data.outcomes || []).map((o) =>
          `<li>${esc(o.opportunity_id || "—")} · ${esc(o.source_id || "")} · ${esc(o.status)} · $${esc(o.contract_value || "—")}</li>`).join("") || "<li>No financing outcomes recorded yet</li>"}</ul>
      </section>`;
    }
    main.innerHTML = `
      <h1>Financing Intelligence</h1>
      <p class="lead">Gather lender terms, approve facts, manage capital, and close deal stacks — without guessing.</p>
      <div class="meta-row">
        <span>Deployable <strong>$${esc(cap.deployable_capital || "0")}</strong></span>
        <span>Reserved <strong>$${esc(d.capital_reserved || "0")}</strong></span>
        <span>Facts to review <strong>${esc(d.facts_awaiting_review || 0)}</strong></span>
        <span>Blocked profit <strong>$${(d.blocked_profit && d.blocked_profit.total_blocked) || "0"}</strong></span>
      </div>
      <div class="deal-actions" style="margin:.75rem 0">${tabs}</div>
      ${body}`;
    main.querySelectorAll("[data-ftab]").forEach((b) => {
      b.onclick = () => navigate("financing", { tab: b.getAttribute("data-ftab") });
    });
    const saveSrc = document.getElementById("fs-save");
    if (saveSrc) {
      saveSrc.onclick = async () => {
        await api("/api/financing/sources", {
          method: "POST",
          body: JSON.stringify({
            company_name: document.getElementById("fs-name").value,
            source_type: document.getElementById("fs-type").value,
            contact_name: document.getElementById("fs-contact").value,
            phone: document.getElementById("fs-phone").value,
            active: true,
          }),
        });
        toast("Lender saved");
        renderFinancing({ tab: "sources" });
      };
    }
    const saveNotes = document.getElementById("fn-save");
    if (saveNotes) {
      saveNotes.onclick = async () => {
        const company = document.getElementById("fn-company").value;
        let sourceId = null;
        if (company && company.startsWith("FS-")) sourceId = company;
        const res = await api("/api/financing/notes", {
          method: "POST",
          body: JSON.stringify({
            raw_notes: document.getElementById("fn-notes").value,
            company_name: company,
            source_id: sourceId,
            contact: document.getElementById("fn-contact").value,
          }),
        });
        toast((res.proposed_facts || []).length + " proposed facts — approve before they affect deals");
        renderFinancing({ tab: "add" });
      };
    }
    const saveCap = document.getElementById("fc-save");
    if (saveCap) {
      saveCap.onclick = async () => {
        await api("/api/financing/capital", {
          method: "POST",
          body: JSON.stringify({
            business_cash: document.getElementById("fc-cash").value,
            minimum_operating_reserve: document.getElementById("fc-reserve").value,
          }),
        });
        toast("Capital updated (still never auto-spent)");
        renderFinancing({ tab: "capital" });
      };
    }
    main.querySelectorAll("[data-fdec]").forEach((b) => {
      b.onclick = async () => {
        const li = b.closest("[data-fid]");
        const fid = li && li.getAttribute("data-fid");
        if (!fid) return;
        await api("/api/financing/facts/" + encodeURIComponent(fid) + "/decide", {
          method: "POST",
          body: JSON.stringify({ decision: b.getAttribute("data-fdec") }),
        });
        toast("Fact " + b.getAttribute("data-fdec").toLowerCase() + "d");
        renderFinancing({ tab });
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
        <h2>Opportunity data</h2>
        <p class="muted" id="opp-health">Loading…</p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Discovery health</h2>
        <p class="muted" id="disc-health">Loading…</p>
        <div id="disc-history" style="margin-top:.75rem"></div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-run-discovery">Run Discovery Now</button>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Discovery Coverage</h2>
        <p class="muted">Raw live universe vs product candidates. Profit filters are downstream.</p>
        <div id="disc-coverage">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-run-expansion">Expand Free Sources</button>
          <span class="muted" style="margin-left:.5rem">BidNet + platform families · no SAM credits</span>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Product / Profit Funnel</h2>
        <p class="muted">Full-universe classification → freshness → evidence-backed profit.</p>
        <div id="universe-funnel">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-run-universe">Run Classification + Profit Pass</button>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Source Coverage — Production</h2>
        <p class="muted">Free national first · BidNet + OpenGov · Euna is PAID_OPTIONAL (not in national health). Twice-daily 06:00 / 14:00.</p>
        <div id="source-coverage-prod">Loading…</div>
        <div id="free-source-roadmap" style="margin-top:.75rem">Loading roadmap…</div>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Connections — BidNet</h2>
        <p class="muted">Authenticated vendor session for twice-daily recovery. Password never shown.</p>
        <div id="bidnet-connection">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-bidnet-test">Test Connection</button>
          <button type="button" class="btn ghost" id="set-bidnet-auth-run" style="margin-left:.35rem">Run BidNet Recovery Now</button>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Connections — OpenGov</h2>
        <p class="muted">Public project-list first; auth only for enrichment. Scope is BROAD_PRODUCT_RESALE (not vendor NAICS prefs). Password never shown.</p>
        <div id="opengov-connection">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-opengov-test">Test Connection</button>
          <button type="button" class="btn ghost" id="set-opengov-discovery" style="margin-left:.35rem">Run OpenGov Discovery Now</button>
          <button type="button" class="btn ghost" id="set-opengov-recovery" style="margin-left:.35rem">Run OpenGov Recovery Now</button>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>Connections — Euna / Bonfire</h2>
        <p class="muted">OPTIONAL — PAID STATE ACCESS. National discovery disabled by default. Connector preserved for targeted states (EUNA_ENABLED_STATES). Password never shown.</p>
        <div id="euna-connection">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-euna-test">Test Connection</button>
          <button type="button" class="btn ghost" id="set-euna-discovery" style="margin-left:.35rem">Run Euna Discovery Now</button>
          <button type="button" class="btn ghost" id="set-euna-recovery" style="margin-left:.35rem">Run Euna Recovery Now</button>
        </p>
      </section>
      <section class="panel" style="margin-top:.75rem">
        <h2>BidNet Recovery</h2>
        <p class="muted">Metadata-only BidNet rows → detail → documents → evidence → economics-ready.</p>
        <div id="bidnet-funnel">Loading…</div>
        <p style="margin-top:.75rem">
          <button type="button" class="btn secondary" id="set-run-bidnet-100">Recover BidNet (100)</button>
          <button type="button" class="btn ghost" id="set-run-bidnet-500" style="margin-left:.35rem">Then 500</button>
        </p>
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
        <p class="muted">Operator path is <strong>/ops</strong> only. Legacy research UIs are not part of the daily workflow.</p>
        <div id="schedule-backed-canary" class="muted" style="margin-top:.5rem">Schedule-backed canary: loading…</div>
        <div id="package-recovery-admin" class="muted" style="margin-top:.75rem">Package recovery: loading…</div>
      </section>`;
    document.getElementById("set-train").onchange = (e) => {
      training = e.target.checked;
      trainingToggle.checked = training;
      localStorage.setItem("m3_training_mode", training ? "1" : "0");
      setHint(training ? "Training Mode on." : "");
    };
    try {
      const h = await api("/api/ui/opportunity-health");
      const el = document.getElementById("opp-health");
      if (el) {
        el.innerHTML = h.status === "DATA_SOURCE_MISSING"
          ? `<strong class="badge red">Missing</strong> ${esc(h.message || "")}<br><span class="muted">${esc(h.store_path || "")}</span>`
          : `Status: <strong>${esc(h.status)}</strong> · Canonical: <strong>${esc(h.canonical_count)}</strong> · Available: <strong>${esc(h.currently_available)}</strong><br>
             Unlocks: <strong>${esc(h.registration_unlocks)}</strong> · Unique blocked: <strong>${esc(h.unique_blocked_opportunities)}</strong><br>
             <span class="muted">Updated: ${esc(h.last_updated || "—")} · ${esc(h.store_path || "")}</span>`;
      }
    } catch (e) {
      const el = document.getElementById("opp-health");
      if (el) el.textContent = e.message || "Could not load opportunity health";
    }
    try {
      const [st, rep] = await Promise.all([
        api("/api/m3/schedule-backed/canary/status").catch(() => ({ status: "NO_STATUS" })),
        api("/api/m3/schedule-backed/canary/report").catch(() => ({ status: "NO_REPORT" })),
      ]);
      const el = document.getElementById("schedule-backed-canary");
      if (el) {
        if (rep.status === "NO_REPORT") {
          el.innerHTML = `<strong>SCHEDULE-BACKED CANARY</strong> · ${esc(st.phase || st.status || "NO_STATUS")} · Selected ${esc(st.selected ?? st.completed ?? "—")}`;
        } else {
          const sel = rep.selection || {};
          const pkg = rep.package || {};
          const sch = rep.schedule_discovery || {};
          const le = rep.line_extraction || {};
          const idn = rep.identity || {};
          const pub = rep.public_pricing || {};
          const rev = rep.revenue || {};
          const g = rep.gates || {};
          el.innerHTML = `<strong>SCHEDULE-BACKED CANARY</strong>
            · Pass: <strong>${esc(g.SCHEDULE_BACKED_PRODUCT_CANARY_PASS ? "YES" : "NO")}</strong><br>
            Selected <strong>${esc(sel.SELECTED ?? rep.canary_input ?? "—")}</strong>
            · Package <strong>${esc(pkg.PACKAGE_READY ?? "—")}</strong>
            · Schedule <strong>${esc(sch.LIKELY_PRODUCT_SCHEDULE ?? "—")}</strong>
            · Lines <strong>${esc(le.OPPORTUNITIES_WITH_LINES ?? "—")}</strong>
            · Identity <strong>${esc(idn.OPPORTUNITIES_WITH_A_E ?? "—")}</strong>
            · Public price <strong>${esc(pub.OPPORTUNITIES_PUBLIC_PRICE_READY ?? "—")}</strong>
            · Revenue <strong>${esc(rev.REVENUE_READY ?? "—")}</strong>
            · CALL_TODAY <strong>${esc(rep.REAL_LIVE_CALL_TODAY ?? 0)}</strong>`;
        }
      }
    } catch (e) {
      const el = document.getElementById("schedule-backed-canary");
      if (el) el.textContent = "Schedule-backed canary: " + (e.message || "unavailable");
    }
    try {
      const [pst, prep] = await Promise.all([
        api("/api/m3/package-materialization/status").catch(() => ({ status: "NO_STATUS" })),
        api("/api/m3/package-materialization/report").catch(() => ({ status: "NO_REPORT" })),
      ]);
      const el = document.getElementById("package-recovery-admin");
      if (el) {
        if (prep.status === "NO_REPORT") {
          el.innerHTML = `<strong>PACKAGE RECOVERY</strong> · ${esc(pst.phase || pst.status || "NO_STATUS")}`;
        } else {
          const a = prep.after || {};
          const g = prep.gates || {};
          const tops = (prep.top_recovered || []).slice(0, 5);
          el.innerHTML = `<strong>PACKAGE RECOVERY</strong>
            · Pass: <strong>${esc((g.PACKAGE_RECOVERY_WORKING || g.NEW_20_PASS) ? "YES" : "NO")}</strong><br>
            Auth docs <strong>${esc(a.AUTHORITATIVE_PRODUCT_DOC_FOUND ?? "—")}</strong>
            · Lines <strong>${esc(a.LINES_READY ?? "—")}</strong>
            · Valid files <strong>${esc(a.VALID_LOCAL_DOCUMENTS ?? "—")}</strong>
            · Discovered <strong>${esc(a.DOCUMENTS_DISCOVERED ?? "—")}</strong>
            <details style="margin-top:.35rem"><summary>Top recovered</summary>
              <ul>${tops.map((t) => `<li>${esc(t.title || t.opportunity || "—")} · ${esc(t.product_schedule)} · lines ${esc(t.extracted_lines ?? 0)} · ${esc(t.blocker || "")}<br><span class="muted">${esc(t.operator_ui_message || "")}</span></li>`).join("")}</ul>
            </details>`;
        }
      }
    } catch (e) {
      const el = document.getElementById("package-recovery-admin");
      if (el) el.textContent = "Package recovery: " + (e.message || "unavailable");
    }
    try {
      const dh = await api("/api/ui/discovery-health");
      const el = document.getElementById("disc-health");
      if (el) {
        const src = `${dh.sources_succeeded ?? 0}/${dh.sources_attempted ?? 0}`;
        el.innerHTML = `Status: <strong>${esc(dh.run_status || "NEVER_RUN")}</strong> · Last run: <strong>${esc(dh.last_attempted_run || "Never")}</strong><br>
          New today: <strong>${esc(dh.new_opportunities_added_today ?? 0)}</strong> · Updated: <strong>${esc(dh.opportunities_updated_today ?? 0)}</strong> · Expired: <strong>${esc(dh.opportunities_expired_removed_today ?? 0)}</strong><br>
          Canonical: <strong>${esc(dh.canonical_opportunities ?? "—")}</strong> · Available: <strong>${esc(dh.currently_available ?? "—")}</strong><br>
          Sources: <strong>${esc(src)}</strong> · SAM: <strong>${esc(dh.sam_calls_used_today ?? "—")}/${esc(dh.sam_calls_limit ?? 10)}</strong><br>
          Next scheduled: <strong>${esc(dh.next_scheduled_run || "—")}</strong>`;
      }
      const hist = document.getElementById("disc-history");
      const days = dh.daily_history || [];
      if (hist) {
        if (!days.length) {
          hist.innerHTML = `<p class="muted">No daily history yet — recorded from this implementation forward.</p>`;
        } else {
          hist.innerHTML = `<table class="simple-table" style="width:100%;font-size:.9rem">
            <thead><tr><th>Date</th><th>Canonical</th><th>Available</th><th>New</th><th>Updated</th><th>Expired</th></tr></thead>
            <tbody>${days.map((d) => `<tr>
              <td>${esc(d.date || "")}</td>
              <td>${esc(d.canonical ?? "")}</td>
              <td>${esc(d.available ?? "")}</td>
              <td>${esc(d.new ?? "")}</td>
              <td>${esc(d.updated ?? "")}</td>
              <td>${esc(d.expired ?? "")}</td>
            </tr>`).join("")}</tbody></table>`;
        }
      }
    } catch (e) {
      const el = document.getElementById("disc-health");
      if (el) el.textContent = e.message || "Could not load discovery health";
    }
    try {
      const cov = await api("/api/ui/discovery-coverage");
      const el = document.getElementById("disc-coverage");
      if (el) {
        const seg = cov.by_segment || {};
        const plats = (cov.platforms || []).slice(0, 12);
        const gaps = (cov.largest_coverage_gaps || []).slice(0, 8);
        el.innerHTML = `
          <div class="meta-row">
            <span>RAW LIVE: <strong>${esc(cov.RAW_LIVE ?? "—")}</strong></span>
            <span>CANONICAL LIVE: <strong>${esc(cov.CANONICAL_LIVE ?? "—")}</strong></span>
            <span>PRODUCT CANDIDATES: <strong>${esc(cov.PRODUCT_CANDIDATES ?? "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Federal: <strong>${esc(seg.Federal ?? 0)}</strong></span>
            <span>State: <strong>${esc(seg.State ?? 0)}</strong></span>
            <span>Local: <strong>${esc(seg.Local ?? 0)}</strong></span>
            <span>Education: <strong>${esc(seg.Education ?? 0)}</strong></span>
            <span>Utilities: <strong>${esc(seg.Utilities ?? 0)}</strong></span>
            <span>Cooperative: <strong>${esc(seg.Cooperative ?? 0)}</strong></span>
            <span>Other: <strong>${esc(seg.Other ?? 0)}</strong></span>
          </div>
          <p class="muted">Target 16,000 · gap <strong>${esc(cov.gap_to_target ?? "—")}</strong> · ${esc(cov.progress_pct ?? 0)}%</p>
          <table class="simple-table" style="width:100%;font-size:.85rem;margin-top:.5rem">
            <thead><tr><th>Platform</th><th>Entities</th><th>Live Opps</th><th>Status</th></tr></thead>
            <tbody>${plats.map((p) => `<tr>
              <td>${esc(p.platform)}</td>
              <td>${esc(p.entities ?? 0)}</td>
              <td>${esc(p.live_opps ?? 0)}</td>
              <td>${esc(p.status)}</td>
            </tr>`).join("")}</tbody>
          </table>
          <h3 style="margin-top:.75rem;font-size:1rem">Largest coverage gaps</h3>
          <ul style="margin:.25rem 0;padding-left:1.1rem;font-size:.9rem">${gaps.map((g) =>
            `<li><strong>${esc(g.platform)}</strong> — entities ${esc(g.known_entities ?? 0)}, live ${esc(g.current_opportunities ?? 0)}, ${esc(g.status)}${g.blocker ? " · " + esc(g.blocker) : ""}</li>`
          ).join("") || "<li class='muted'>None flagged</li>"}</ul>
          ${(cov.authentication_blockers || []).length ? `<p class="muted">Free account required — connect/login to expand coverage (${esc((cov.authentication_blockers || []).length)} platforms).</p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("disc-coverage");
      if (el) el.textContent = e.message || "Could not load discovery coverage";
    }
    try {
      const uf = await api("/api/ui/universe-funnel");
      const el = document.getElementById("universe-funnel");
      if (el) {
        const src = ((uf.last_pass || {}).by_source || []).slice(0, 8);
        el.innerHTML = `
          <div class="meta-row">
            <span>Canonical Live: <strong>${esc(uf.Canonical_Live ?? "—")}</strong></span>
            <span>Product: <strong>${esc(uf.Product_Candidates ?? 0)}</strong></span>
            <span>Mixed: <strong>${esc(uf.Mixed_Product_Candidates ?? 0)}</strong></span>
            <span>Services: <strong>${esc(uf.Pure_Services ?? 0)}</strong></span>
            <span>Construction: <strong>${esc(uf.Construction ?? 0)}</strong></span>
            <span>Unknown: <strong>${esc(uf.Unknown_Classification ?? 0)}</strong></span>
          </div>
          <div class="meta-row">
            <span>Freshness unknown: <strong>${esc(uf.Freshness_Unknown ?? 0)}</strong></span>
            <span>Two-sided economics: <strong>${esc(uf.Two_Sided_Economics ?? 0)}</strong></span>
            <span>Public retail profit: <strong>${esc(uf.Profitable_at_Public_Retail ?? 0)}</strong></span>
            <span>Proven: <strong>${esc(uf.Proven_Profitable ?? 0)}</strong></span>
            <span>Likely: <strong>${esc(uf.Likely_Profitable ?? 0)}</strong></span>
            <span>Possible: <strong>${esc(uf.Possible_Profit ?? 0)}</strong></span>
          </div>
          ${src.length ? `<table class="simple-table" style="width:100%;font-size:.85rem;margin-top:.5rem">
            <thead><tr><th>Source</th><th>Live</th><th>Product</th><th>Mixed</th><th>Service</th><th>Unknown</th><th>Stale</th></tr></thead>
            <tbody>${src.map((r) => `<tr>
              <td>${esc(r.source)}</td><td>${esc(r.live)}</td><td>${esc(r.product)}</td>
              <td>${esc(r.mixed)}</td><td>${esc(r.service)}</td><td>${esc(r.unknown)}</td><td>${esc(r.stale)}</td>
            </tr>`).join("")}</tbody></table>` : `<p class="muted">Run the classification pass to populate source quality table.</p>`}`;
      }
    } catch (e) {
      const el = document.getElementById("universe-funnel");
      if (el) el.textContent = e.message || "Could not load universe funnel";
    }
    try {
      const sc = await api("/api/m3/source-coverage/production");
      const el = document.getElementById("source-coverage-prod");
      if (el) {
        const bn = (sc.owner || {}).bidnet || {};
        const og = (sc.owner || {}).opengov || {};
        const eu = (sc.owner || {}).euna || {};
        const comb = sc.combined || {};
        const nh = sc.national_source_health || {};
        el.innerHTML = `
          <div class="meta-row">
            <span>Build: <strong>${esc(sc.build_version || "—")}</strong></span>
            <span>Canonical live: <strong>${esc(comb.canonical_live ?? "—")}</strong></span>
            <span>Target: <strong>${esc(comb.target_canonical_live ?? 50000)}</strong></span>
            <span>Free net-new: <strong>${esc(comb.free_source_net_new_this_run ?? comb.net_new_this_run ?? "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>National health: BidNet <strong>${esc(nh.bidnet_ok ? "ok" : "—")}</strong></span>
            <span>OpenGov: <strong>in repair</strong></span>
            <span>Euna affects score: <strong>no</strong></span>
          </div>
          <p style="margin:.5rem 0 .25rem"><strong>BIDNET</strong> <span class="badge green">FREE_ACTIVE</span></p>
          <div class="meta-row">
            <span>Reported: <strong>${esc(bn.reported_open ?? "—")}</strong></span>
            <span>Retrieved: <strong>${esc(bn.retrieved ?? "—")}</strong></span>
            <span>Retrieval %: <strong>${esc(bn.retrieval_pct != null ? bn.retrieval_pct + "%" : "—")}</strong></span>
            <span>Pagination: <strong>${esc(bn.pagination_complete ? "complete" : "open")}</strong></span>
            <span>Net-new: <strong>${esc(bn.net_new ?? "—")}</strong></span>
            <span>Gap: <strong>${esc(bn.remaining_gap ?? "—")}</strong></span>
          </div>
          <p style="margin:.5rem 0 .25rem"><strong>OPENGOV</strong> <span class="badge orange">FREE_PARTIAL</span> — primary repair</p>
          <div class="meta-row">
            <span>Known: <strong>${esc(og.known_entities ?? "—")}</strong></span>
            <span>Attempted: <strong>${esc(og.attempted ?? "—")}</strong></span>
            <span>Public working: <strong>${esc(og.public_working ?? "—")}</strong></span>
            <span>Auth working: <strong>${esc(og.auth_working ?? "—")}</strong></span>
            <span>Anti-bot: <strong>${esc(og.anti_bot ?? "—")}</strong></span>
            <span>Raw: <strong>${esc(og.raw ?? "—")}</strong></span>
            <span>Net-new: <strong>${esc(og.net_new ?? "—")}</strong></span>
          </div>
          <p style="margin:.5rem 0 .25rem"><strong>EUNA</strong> <span class="badge gray">PAID_OPTIONAL</span></p>
          <div class="meta-row">
            <span>Status: <strong>${esc(eu.owner_label || eu.status || "OPTIONAL — PAID STATE ACCESS")}</strong></span>
            <span>National discovery: <strong>${esc(eu.national_discovery_enabled ? "ENABLED" : "DISABLED")}</strong></span>
            <span>Enabled states: <strong>${esc(eu.enabled_states || "NONE")}</strong></span>
          </div>
          ${eu.blocker ? `<p class="muted" style="margin-top:.5rem">Euna note: <strong>${esc(eu.blocker)}</strong> (excluded from national health)</p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("source-coverage-prod");
      if (el) el.textContent = e.message || "Could not load source coverage";
    }
    try {
      const rm = await api("/api/m3/source-coverage/roadmap");
      const el = document.getElementById("free-source-roadmap");
      if (el) {
        const rows = rm.sources || [];
        el.innerHTML = `
          <p style="margin:0 0 .35rem"><strong>Free-source roadmap</strong> · strategy ${esc(rm.strategy || "FREE_NATIONAL")} · primary repair: <strong>${esc(rm.primary_repair_target || "OpenGov")}</strong></p>
          <table class="data"><thead><tr><th>Source</th><th>Category</th><th>Priority</th><th>Next</th></tr></thead>
          <tbody>${rows.map(r => `<tr>
            <td>${esc(r.source)}</td>
            <td><strong>${esc(r.category)}</strong></td>
            <td>${esc(r.priority)}</td>
            <td class="muted">${esc(r.next_action || "")}</td>
          </tr>`).join("")}</tbody></table>`;
      }
    } catch (e) {
      const el = document.getElementById("free-source-roadmap");
      if (el) el.textContent = e.message || "Could not load roadmap";
    }
    try {
      const bc = await api("/api/ui/bidnet-auth/status");
      const el = document.getElementById("bidnet-connection");
      if (el) {
        const st = bc.status || "UNKNOWN";
        const badge =
          st === "CONNECTED" || st === "SESSION_REUSED" || st === "LOGIN_SUCCESS" ? "green" :
          st === "AUTH_CHALLENGE" ? "orange" :
          st === "DISABLED" ? "gray" : "red";
        const tel = bc.telemetry || {};
        el.innerHTML = `
          <div class="meta-row">
            <span>Status: <strong class="badge ${badge}">${esc(st)}</strong></span>
            <span>Credentials: <strong>${esc(bc.credentials_configured ? "configured" : "missing")}</strong></span>
            <span>Session file: <strong>${esc(bc.storage_state_present ? "present" : "none")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Last login: <strong>${esc(bc.last_successful_login || "—")}</strong></span>
            <span>Last session reuse: <strong>${esc(bc.last_session_reuse || "—")}</strong></span>
            <span>Last auth run: <strong>${esc(bc.last_authenticated_run || "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Reported open: <strong>${esc(bc.reported_open ?? "—")}</strong></span>
            <span>Harvested: <strong>${esc(bc.harvested ?? 0)}</strong></span>
            <span>Pagination: <strong>${esc(bc.pagination_pct != null ? (bc.pagination_pct + "%") : "—")}</strong>${bc.pagination_complete ? " ✓" : (bc.discovery_truncated ? " truncated" : "")}</span>
          </div>
          <div class="meta-row">
            <span>Details recovered: <strong>${esc(bc.details_recovered ?? 0)}</strong></span>
            <span>Documents recovered: <strong>${esc(bc.documents_recovered ?? 0)}</strong></span>
            <span>Economics ready: <strong>${esc(bc.economics_ready ?? 0)}</strong></span>
          </div>
          <div class="meta-row">
            <span>Existing enriched: <strong>${esc(bc.existing_enriched ?? 0)}</strong></span>
            <span>Net-new unique: <strong>${esc(bc.net_new_unique ?? 0)}</strong></span>
            <span>Last harvest: <strong>${esc(bc.last_harvest_at || "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Logins: ${esc(tel.login_successes ?? 0)}/${esc(tel.login_attempts ?? 0)}</span>
            <span>Reuses: ${esc(tel.session_reuses ?? 0)}</span>
            <span>Challenges: ${esc(tel.auth_challenges ?? 0)}</span>
          </div>
          ${bc.last_failure_reason ? `<p class="muted" style="margin-top:.5rem">Last failure: <strong>${esc(bc.last_failure_reason)}</strong></p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("bidnet-connection");
      if (el) el.textContent = e.message || "Could not load BidNet connection status";
    }
    try {
      const oc = await api("/api/ui/opengov-auth/status");
      const el = document.getElementById("opengov-connection");
      if (el) {
        const st = oc.status || "NOT_CONNECTED";
        const badge =
          st === "CONNECTED" || st === "SESSION_REUSED" || st === "LOGIN_SUCCESS" ? "green" :
          st === "AUTH_CHALLENGE" ? "orange" :
          st === "DISABLED" || st === "NOT_CONNECTED" ? "gray" : "red";
        const tel = oc.telemetry || {};
        const rates = oc.rates || {};
        el.innerHTML = `
          <div class="meta-row">
            <span>Status: <strong class="badge ${badge}">${esc(st)}</strong></span>
            <span>Authenticated: <strong>${esc(oc.authenticated ? "yes" : "no")}</strong></span>
            <span>Credentials: <strong>${esc(oc.credentials_configured ? "configured" : "missing")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Last login: <strong>${esc(oc.last_successful_login || "—")}</strong></span>
            <span>Last session reuse: <strong>${esc(oc.last_session_reuse || "—")}</strong></span>
            <span>Last discovery: <strong>${esc(oc.last_discovery_run || "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Known agencies: <strong>${esc(oc.known_entities ?? 0)}</strong></span>
            <span>Producing: <strong>${esc(oc.entities_successful ?? 0)}</strong></span>
            <span>Raw opps: <strong>${esc(oc.raw_opportunities ?? 0)}</strong></span>
            <span>Canonical live: <strong>${esc(oc.canonical_live ?? 0)}</strong></span>
          </div>
          <div class="meta-row">
            <span>Documents: <strong>${esc(oc.documents_recovered ?? 0)}</strong></span>
            <span>Product candidates: <strong>${esc(oc.product_candidates ?? 0)}</strong></span>
            <span>Economics ready: <strong>${esc(oc.economics_ready ?? 0)}</strong></span>
          </div>
          <div class="meta-row">
            <span>Detail rate: ${esc(rates.detail ?? 0)}%</span>
            <span>Doc rate: ${esc(rates.documents ?? 0)}%</span>
            <span>Ready rate: ${esc(rates.economics_ready ?? 0)}%</span>
            <span>Logins: ${esc(tel.login_successes ?? 0)}/${esc(tel.login_attempts ?? 0)}</span>
          </div>
          ${oc.account_category_restriction ? `<p class="muted" style="margin-top:.5rem"><strong>ACCOUNT_CATEGORY_RESTRICTION</strong> detected — M3 still uses BROAD_PRODUCT_RESALE scope.</p>` : ""}
          ${oc.last_failure_reason ? `<p class="muted" style="margin-top:.5rem">Last failure: <strong>${esc(oc.last_failure_reason)}</strong></p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("opengov-connection");
      if (el) el.textContent = e.message || "Could not load OpenGov connection status";
    }
    try {
      const ec = await api("/api/ui/euna-auth/status");
      const el = document.getElementById("euna-connection");
      if (el) {
        const st = ec.status || "PAID_OPTIONAL";
        const badge =
          st === "PAID_OPTIONAL" || st === "DISABLED" ? "gray" :
          st === "CONNECTED" || st === "SESSION_REUSED" || st === "LOGIN_SUCCESS" || st === "WORKING_AUTH" ? "green" :
          st === "AUTH_CHALLENGE" ? "orange" :
          st === "NOT_CONNECTED" ? "gray" : "orange";
        const hy = ec.credential_hygiene || {};
        el.innerHTML = `
          <div class="meta-row">
            <span>Status: <strong class="badge ${badge}">${esc(ec.owner_label || "OPTIONAL — PAID STATE ACCESS")}</strong></span>
            <span>Category: <strong>${esc(ec.coverage_category || "PAID_OPTIONAL")}</strong></span>
            <span>Role: <strong>${esc(ec.source_role || "OPTIONAL_TARGETED_SOURCE")}</strong></span>
          </div>
          <div class="meta-row">
            <span>National discovery: <strong>${esc(ec.national_discovery_enabled ? "ENABLED" : "DISABLED")}</strong></span>
            <span>Enabled states: <strong>${esc(ec.enabled_states_display || "NONE")}</strong></span>
            <span>Auth status: <strong>${esc(ec.auth_status || "—")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Credentials: <strong>${esc(ec.credentials_configured ? "configured" : "missing")}</strong></span>
            <span>User len: <strong>${esc(hy.username_length ?? "—")}</strong></span>
            <span>Pass len: <strong>${esc(hy.password_length ?? "—")}</strong></span>
            <span>Session file: <strong>${esc(ec.storage_state_present ? "present" : "none")}</strong></span>
          </div>
          <div class="meta-row">
            <span>Last login: <strong>${esc(ec.last_successful_login || "—")}</strong></span>
            <span>Session reuse: <strong>${esc(ec.last_session_reuse || "—")}</strong></span>
            <span>Last discovery: <strong>${esc(ec.last_discovery_run || "—")}</strong></span>
          </div>
          <p class="muted" style="margin-top:.5rem">Not counted in national source health. Manual Test/Discovery still available for future targeted states.</p>
          ${ec.last_failure_reason ? `<p class="muted" style="margin-top:.5rem">Last auth note: <strong>${esc(ec.last_failure_reason)}</strong></p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("euna-connection");
      if (el) el.textContent = e.message || "Could not load Euna connection status";
    }
    try {
      const bf = await api("/api/ui/bidnet-recovery/funnel");
      const el = document.getElementById("bidnet-funnel");
      if (el) {
        const pct = bf.conversion_pct || {};
        el.innerHTML = `
          <div class="meta-row">
            <span>BidNet discovered: <strong>${esc(bf.BidNet_discovered ?? 0)}</strong></span>
            <span>Detail: <strong>${esc(bf.Detail_recovered ?? 0)}</strong> (${esc(pct.detail_of_discovered ?? 0)}%)</span>
            <span>Documents: <strong>${esc(bf.Documents_recovered ?? 0)}</strong></span>
            <span>Product ID: <strong>${esc(bf.Product_identified ?? 0)}</strong></span>
          </div>
          <div class="meta-row">
            <span>Gov value: <strong>${esc(bf.Gov_value_found ?? 0)}</strong></span>
            <span>Public cost: <strong>${esc(bf.Public_cost_found ?? 0)}</strong></span>
            <span>Economics ready: <strong>${esc(bf.Economics_ready ?? 0)}</strong></span>
            <span>Profitable: <strong>${esc(bf.Profitable ?? 0)}</strong></span>
          </div>
          ${(bf.blockers && Object.keys(bf.blockers).length) ? `<p class="muted" style="margin-top:.5rem">Top blockers: ${esc(Object.entries(bf.blockers).slice(0,5).map(([k,v]) => k+": "+v).join(" · "))}</p>` : ""}`;
      }
    } catch (e) {
      const el = document.getElementById("bidnet-funnel");
      if (el) el.textContent = e.message || "Could not load BidNet funnel";
    }
    document.getElementById("set-run-discovery")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-run-discovery");
      if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
      try {
        const res = await api("/api/ui/discovery/run", { method: "POST", body: "{}" });
        setHint(res.already_running ? "Discovery already running." : (res.accepted ? ("Started " + (res.run_id || "")) : (res.message || "Not started")));
        setTimeout(() => renderSettings(), 2000);
      } catch (e) {
        setHint(e.message || "Run failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run Discovery Now"; }
      }
    });
    document.getElementById("set-run-expansion")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-run-expansion");
      if (btn) { btn.disabled = true; btn.textContent = "Harvesting…"; }
      setHint("Expansion harvest started — BidNet pagination can take several minutes.");
      try {
        const res = await api("/api/ui/discovery-expansion/harvest", { method: "POST", body: "{}" });
        setHint(
          "Expansion done: raw " + (res.raw_discovered ?? res.raw_opportunities ?? "?") +
          " · unique " + (res.unique_records ?? "?") +
          " · live universe " + (res.TOTAL_LIVE_DISCOVERY_UNIVERSE ?? "?")
        );
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "Expansion harvest failed");
        if (btn) { btn.disabled = false; btn.textContent = "Expand Free Sources"; }
      }
    });
    document.getElementById("set-run-universe")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-run-universe");
      if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
      setHint("Universe classification + profit pass started…");
      try {
        const res = await api("/api/ui/universe-pass/run", {
          method: "POST",
          body: JSON.stringify({ force_reclassify: true }),
        });
        const c = res.classification || {};
        setHint(
          "Live " + (res.after_canonical_live ?? "?") +
          " · Product " + (c.TANGIBLE_PRODUCT ?? 0) +
          " · Mixed " + (c.MIXED_PRODUCT_SERVICE ?? 0) +
          " · Proven " + ((res.product_economics || {}).proven_profitable ?? 0)
        );
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "Universe pass failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run Classification + Profit Pass"; }
      }
    });
    const runBidnet = async (limit) => {
      const btn = document.getElementById(limit <= 100 ? "set-run-bidnet-100" : "set-run-bidnet-500");
      if (btn) { btn.disabled = true; btn.textContent = "Recovering…"; }
      setHint("BidNet recovery started (limit " + limit + ")…");
      try {
        const res = await api("/api/ui/bidnet-recovery/run", {
          method: "POST",
          body: JSON.stringify({ limit, resume: true }),
        });
        const st = res.stats || {};
        setHint(
          "BidNet: detail " + (st.detail_recovered ?? 0) +
          " · deadlines " + (st.deadlines_recovered ?? 0) +
          " · auth wall " + (st.auth_wall ?? 0) +
          " · ready " + (st.economics_ready ?? 0)
        );
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "BidNet recovery failed");
        if (btn) { btn.disabled = false; btn.textContent = limit <= 100 ? "Recover BidNet (100)" : "Then 500"; }
      }
    };
    document.getElementById("set-run-bidnet-100")?.addEventListener("click", () => runBidnet(100));
    document.getElementById("set-run-bidnet-500")?.addEventListener("click", () => runBidnet(500));
    document.getElementById("set-bidnet-test")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-bidnet-test");
      if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
      setHint("BidNet Test Connection — validating session / automatic login…");
      try {
        const res = await api("/api/ui/bidnet-auth/test-connection", { method: "POST", body: "{}" });
        setHint(
          "BidNet auth: " + (res.status || "?") +
          (res.reused_session ? " (session reused)" : "") +
          (res.message ? " — " + res.message : "")
        );
        setTimeout(() => renderSettings(), 1200);
      } catch (e) {
        setHint(e.message || "BidNet test connection failed");
        if (btn) { btn.disabled = false; btn.textContent = "Test Connection"; }
      }
    });
    document.getElementById("set-bidnet-auth-run")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-bidnet-auth-run");
      if (btn) { btn.disabled = true; btn.textContent = "Running…"; }
      setHint("Authenticated BidNet recovery started…");
      try {
        const res = await api("/api/ui/bidnet-recovery/run", {
          method: "POST",
          body: JSON.stringify({ limit: 100, use_auth: true, resume: true }),
        });
        if (res.auth_stop) {
          setHint("BidNet AUTH STOPPED: " + res.auth_stop + " — " + ((res.auth || {}).message || ""));
        } else {
          const st = res.stats || {};
          setHint(
            "BidNet auth recovery: detail " + (st.detail_recovered ?? 0) +
            " · docs " + (st.documents_recovered ?? 0) +
            " · org " + (st.issuing_org_recovered ?? 0) +
            " · sol# " + (st.solicitation_number_recovered ?? 0)
          );
        }
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "Authenticated BidNet recovery failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run BidNet Recovery Now"; }
      }
    });
    document.getElementById("set-opengov-test")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-opengov-test");
      if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
      setHint("OpenGov Test Connection — validating session / automatic login…");
      try {
        const res = await api("/api/ui/opengov-auth/test-connection", { method: "POST", body: "{}" });
        setHint(
          "OpenGov auth: " + (res.status || "?") +
          (res.reused_session ? " (session reused)" : "") +
          (res.message ? " — " + res.message : "")
        );
        setTimeout(() => renderSettings(), 1200);
      } catch (e) {
        setHint(e.message || "OpenGov test connection failed");
        if (btn) { btn.disabled = false; btn.textContent = "Test Connection"; }
      }
    });
    document.getElementById("set-euna-test")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-euna-test");
      if (btn) { btn.disabled = true; btn.textContent = "Testing…"; }
      setHint("Euna auth diagnostic started (async) — poll Settings shortly…");
      try {
        const res = await api("/api/ui/euna-auth/test-connection", { method: "POST", body: "{}" });
        setHint(res.accepted ? ("Euna diagnostic job " + (res.job_id || "") + " queued") : (res.message || "Started"));
        setTimeout(() => renderSettings(), 4000);
      } catch (e) {
        setHint(e.message || "Euna test connection failed");
        if (btn) { btn.disabled = false; btn.textContent = "Test Connection"; }
      }
    });
    document.getElementById("set-euna-discovery")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-euna-discovery");
      if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
      setHint("Euna central Supplier Network discovery started (async)…");
      try {
        const res = await api("/api/ui/euna-discovery/run", {
          method: "POST",
          body: JSON.stringify({ mode: "central", max_results: 5000, max_pages: 40 }),
        });
        setHint(res.accepted ? ("Euna job " + (res.job_id || "") + " queued") : (res.message || "Started"));
        setTimeout(() => renderSettings(), 2000);
      } catch (e) {
        setHint(e.message || "Euna discovery failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run Euna Discovery Now"; }
      }
    });
    document.getElementById("set-euna-recovery")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-euna-recovery");
      if (btn) { btn.disabled = true; btn.textContent = "Starting…"; }
      setHint("Euna recovery (central, limited batch) started…");
      try {
        const res = await api("/api/ui/euna-recovery/run", {
          method: "POST",
          body: JSON.stringify({ max_results: 100, max_pages: 10 }),
        });
        setHint(res.accepted ? ("Euna recovery job " + (res.job_id || "") + " queued") : (res.message || "Started"));
        setTimeout(() => renderSettings(), 2000);
      } catch (e) {
        setHint(e.message || "Euna recovery failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run Euna Recovery Now"; }
      }
    });
    document.getElementById("set-opengov-discovery")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-opengov-discovery");
      if (btn) { btn.disabled = true; btn.textContent = "Discovering…"; }
      setHint("OpenGov cascade discovery started (public→auth→agency; ANTI_BOT non-terminal)…");
      try {
        const res = await api("/api/ui/opengov-discovery/run", {
          method: "POST",
          body: JSON.stringify({ max_entities: "all", max_pages: 6, use_auth: true, mode: "cascade", async: true }),
        });
        if (res.accepted && res.job_id) {
          setHint("OpenGov cascade job " + res.job_id + " queued — poll Settings for results.");
        } else if (res.blocker) {
          setHint("OpenGov discovery blocked: " + res.blocker + " — " + ((res.auth || {}).message || ""));
        } else {
          const cm = res.canonical_merge || {};
          setHint(
            "OpenGov: raw " + (res.raw_opportunities ?? 0) +
            " · unique " + (res.unique_records ?? 0) +
            " · net-new " + (cm.new ?? res.net_new ?? 0) +
            " · ok " + (res.entities_successful ?? 0) + "/" + (res.entities_attempted ?? 0) +
            " · anti-bot recovered " + (res.anti_bot_recovered_via_fallback ?? 0)
          );
        }
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "OpenGov discovery failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run OpenGov Discovery Now"; }
      }
    });
    document.getElementById("set-opengov-recovery")?.addEventListener("click", async () => {
      const btn = document.getElementById("set-opengov-recovery");
      if (btn) { btn.disabled = true; btn.textContent = "Recovering…"; }
      setHint("OpenGov authenticated recovery started…");
      try {
        const res = await api("/api/ui/opengov-recovery/run", {
          method: "POST",
          body: JSON.stringify({ limit: 100, use_auth: true, resume: true }),
        });
        if (res.auth_stop) {
          setHint("OpenGov AUTH STOPPED: " + res.auth_stop + " — " + ((res.auth || {}).message || ""));
        } else {
          const st = res.stats || {};
          setHint(
            "OpenGov recovery: detail " + (st.improved ?? 0) +
            " · docs " + (st.documents_recovered ?? 0) +
            " · ready " + (st.economics_ready ?? 0)
          );
        }
        setTimeout(() => renderSettings(), 1500);
      } catch (e) {
        setHint(e.message || "OpenGov recovery failed");
        if (btn) { btn.disabled = false; btn.textContent = "Run OpenGov Recovery Now"; }
      }
    });
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
      else if (path === "channel-tests") await renderChannelTests();
      else if (path === "large-test") await renderLargeTest();
      else if (path === "package-recovery") await renderPackageRecovery();
      else if (path === "bidnet-production") await renderBidnetProduction();
      else if (path === "bidnet-downstream") await renderBidnetDownstream();
      else if (path === "bidnet-engine") await renderBidnetEngine();
      else if (path === "quote") await renderQuote(params);
      else if (path === "registrations") await renderRegistrations();
      else if (path === "registration") await renderRegistration(params);
      else if (path === "registration-opps") await renderRegistrationOpps(params);
      else if (path === "blocked") await renderBlocked(params);
      else if (path === "watch") await renderWatch(params);
      else if (path === "bid-prep") await renderBidPrep();
      else if (path === "financing") await renderFinancing(params);
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
