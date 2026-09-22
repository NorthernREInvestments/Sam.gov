/** Micro-Purchase Economics Lab — operator UI (backend authoritative). */
(function () {
  let current = null;

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, opts) {
    const fetchFn = typeof apiFetch === "function" ? apiFetch : fetch;
    const res = await fetchFn(path, {
      headers: { Accept: "application/json", "Content-Type": "application/json", ...(opts && opts.headers) },
      ...opts,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || JSON.stringify(j);
      } catch (_) {}
      throw new Error(detail || "request failed");
    }
    return res.json();
  }

  function showTab(name) {
    document.querySelectorAll(".mpl-tab").forEach((b) => b.classList.toggle("active", b.dataset.mplTab === name));
    ["queue", "tests", "quotes", "today", "dashboard"].forEach((t) => {
      const el = document.getElementById("mpl-panel-" + t);
      if (el) el.hidden = t !== name;
    });
    if (name === "queue") loadQueue();
    if (name === "tests") loadTests();
    if (name === "quotes") loadQuoteQueue();
    if (name === "today") loadToday();
    if (name === "dashboard") loadDashboard();
  }

  async function loadQueue() {
    const list = document.getElementById("mpl-queue-list");
    const meta = document.getElementById("mpl-queue-meta");
    if (list) list.innerHTML = "<p class='muted'>Loading queue…</p>";
    try {
      const data = await api("/api/m3/micro-purchase-lab/queue?limit=40");
      if (meta) meta.textContent = (data.count || 0) + " candidates · " + (data.note || "");
      if (!list) return;
      if (!(data.items || []).length) {
        list.innerHTML = "<p class='muted'>No micro/near-micro product candidates in pipeline yet. Create a manual test.</p>";
        return;
      }
      list.innerHTML = data.items
        .map(
          (it) => `<article class="m3-info-card">
          <h3>${esc(it.product_title)}</h3>
          <p class="muted">${esc(it.source)} · ${esc(it.solicitation || "—")} · ${esc(it.classification)}</p>
          <p>Agency: ${esc(it.agency || "—")} · NSN: ${esc(it.nsn || "—")} · P/N: ${esc(it.part_number || "—")}</p>
          <p>Size: ${esc(it.estimated_opportunity_size)} · Deadline: ${esc(it.deadline || "UNKNOWN")} · Runway: ${esc(it.deadline_runway ?? "—")}</p>
          <p>Hist $: ${esc(it.last_government_award_price)} · Quotes: ${esc(it.quote_status)} · ${esc(it.next_action)}</p>
          <div class="m3-btn-row">
            <button type="button" class="m3-back-btn mpl-load-cid" data-cid="${esc(it.canonical_id)}">Load into Lab</button>
            ${it.detail_url ? `<a class="m3-back-btn" href="${esc(it.detail_url)}" target="_blank" rel="noopener">Open source</a>` : ""}
          </div>
        </article>`
        )
        .join("");
      list.querySelectorAll(".mpl-load-cid").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const row = await api("/api/m3/micro-purchase-lab/load-opportunity", {
            method: "POST",
            body: JSON.stringify({ canonical_id: btn.dataset.cid }),
          });
          current = row;
          showTab("tests");
          openEditor(row);
        });
      });
    } catch (e) {
      if (list) list.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  async function loadTests() {
    const list = document.getElementById("mpl-tests-list");
    if (list) list.innerHTML = "<p class='muted'>Loading tests…</p>";
    try {
      const data = await api("/api/m3/micro-purchase-lab/tests");
      if (!list) return;
      if (!(data.tests || []).length) {
        list.innerHTML = "<p class='muted'>No saved validation tests yet.</p>";
        return;
      }
      list.innerHTML = data.tests
        .map(
          (t) => `<article class="m3-info-card">
          <h3>${esc(t.product || t.solicitation || t.id)}</h3>
          <p><span class="mpl-pill">${esc(t.status)}</span> · ${esc(t.classification)} · ${esc(t.source || "")}</p>
          <p class="muted">${esc(t.agency || "")} · Qty ${esc(t.quantity || "—")} ${esc(t.government_unit || "")}</p>
          <button type="button" class="m3-back-btn mpl-open-test" data-id="${esc(t.id)}">Open</button>
        </article>`
        )
        .join("");
      list.querySelectorAll(".mpl-open-test").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const row = await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(btn.dataset.id));
          current = row;
          openEditor(row);
        });
      });
    } catch (e) {
      if (list) list.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  async function loadDashboard() {
    const body = document.getElementById("mpl-dashboard-body");
    if (body) body.innerHTML = "<p class='muted'>Loading dashboard…</p>";
    try {
      const d = await api("/api/m3/micro-purchase-lab/dashboard");
      const g = d.go_no_go || {};
      const m = d.micro_purchase_results || {};
      const a = d.all_results || {};
      const auto = d.automation || {};
      const perf = (auto.supplier_performance || [])
        .map(
          (s) =>
            `<li>${esc(s.supplier)} · n=${esc(s.observation_count)} · med disc ${esc(s.median_discount_pct ?? "—")}% · exec ${esc(s.executable_quote_count)}${s.statistically_meaningful ? "" : " · (sample small)"}</li>`
        )
        .join("");
      if (!body) return;
      body.innerHTML = `
        <article class="m3-info-card">
          <h3>Go / No-Go tracker</h3>
          <p class="mpl-progress">${esc(g.progress_label)}</p>
          <p>Profitable: ${esc(g.profitable_quotes)} · Unprofitable: ${esc(g.unprofitable_quotes)} · Execution fails: ${esc(g.execution_failures)} · Bid candidates: ${esc(g.executable_bid_candidates)}</p>
          <p class="muted">${esc(g.note)}</p>
        </article>
        <article class="m3-info-card">
          <h3>Automated research</h3>
          <p>Research completed: ${esc(auto.automated_research_completed ?? 0)} · Research→Quote: ${esc(auto.research_to_quote_conversion ?? "—")}</p>
          <p>Quotes requested: ${esc(auto.quotes_requested ?? 0)} · Quotes received: ${esc(auto.quotes_received ?? 0)}</p>
          <p>Avg supplier discount vs public: ${esc(auto.average_supplier_discount_vs_public ?? "—")}% · Median: ${esc(auto.median_supplier_discount ?? "—")}%</p>
          <p>Econ pass rate: ${esc(auto.economic_pass_rate ?? "—")} · Exec pass: ${esc(auto.execution_pass_rate ?? "—")} · BID_CANDIDATE rate: ${esc(auto.bid_candidate_rate ?? "—")}</p>
          <p>Median GP: ${esc(auto.median_gross_profit ?? "—")} · Median margin: ${esc(auto.median_gross_margin ?? "—")}%</p>
          <h4>Supplier performance</h4>
          <ul>${perf || "<li class='muted'>No quote intelligence yet</li>"}</ul>
        </article>
        <article class="m3-info-card">
          <h3>Micro-Purchase Results</h3>
          <p>Tested: ${esc(m.opportunities_tested)} · Hist reconstructed: ${esc(m.historical_economics_reconstructed)}</p>
          <p>Quotes recv: ${esc(m.supplier_quotes_received)} · Econ pass: ${esc(m.economic_passes)} · Econ fail: ${esc(m.economic_fails)}</p>
          <p>Bid candidates: ${esc(m.bid_candidates)} · Quote pass rate: ${esc(m.quote_pass_rate ?? "—")} · Bid cand rate: ${esc(m.overall_bid_candidate_rate ?? "—")}</p>
          <p>Median GP: ${esc(m.median_gross_profit ?? "—")} · Median margin: ${esc(m.median_gross_margin_pct ?? "—")}% · Total potential GP: ${esc(m.total_potential_gross_profit ?? "—")}</p>
        </article>
        <article class="m3-info-card">
          <h3>All tests (incl. comparison)</h3>
          <p>Tested: ${esc(a.opportunities_tested)} · Bid candidates: ${esc(a.bid_candidates)}</p>
        </article>`;
    } catch (e) {
      if (body) body.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  async function loadQuoteQueue() {
    const list = document.getElementById("mpl-quote-queue-list");
    if (list) list.innerHTML = "<p class='muted'>Loading quote queue…</p>";
    try {
      const data = await api("/api/m3/micro-purchase-lab/quote-queue");
      if (!list) return;
      if (!(data.items || []).length) {
        list.innerHTML = "<p class='muted'>No prepared quote requests yet. Research an opportunity, then Prepare Quotes.</p>";
        return;
      }
      list.innerHTML = data.items
        .map(
          (it) => `<article class="m3-info-card">
          <h3>${esc(it.supplier)} · ${esc(it.part_number || it.product || "—")}</h3>
          <p><span class="mpl-pill">${esc(it.quote_status)}</span> · Qty ${esc(it.quantity || "—")} · Deadline ${esc(it.deadline || "—")}</p>
          <p>HE bid: ${esc(it.historical_equivalent_price ?? "—")} · Public: ${esc(it.current_public_price ?? "—")} · Est band: ${esc(it.estimated_quote_value_band ?? "—")}</p>
          <p class="muted">${esc(it.opportunity || it.solicitation || "")} · Next: ${esc(it.next_action || "—")}</p>
          <div class="m3-btn-row">
            <button type="button" class="m3-back-btn mpl-qq-copy" data-text="${esc(it.quote_request_text || "")}">Copy request</button>
            <button type="button" class="m3-back-btn mpl-qq-req" data-id="${esc(it.id)}">Mark requested</button>
            ${it.test_id ? `<button type="button" class="m3-back-btn mpl-qq-open" data-id="${esc(it.test_id)}">Open test</button>` : ""}
          </div>
        </article>`
        )
        .join("");
      list.querySelectorAll(".mpl-qq-copy").forEach((btn) => {
        btn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(btn.dataset.text || "");
          } catch (_) {}
        });
      });
      list.querySelectorAll(".mpl-qq-req").forEach((btn) => {
        btn.addEventListener("click", async () => {
          await api("/api/m3/micro-purchase-lab/quote-queue/" + encodeURIComponent(btn.dataset.id) + "/status", {
            method: "POST",
            body: JSON.stringify({ quote_status: "REQUESTED" }),
          });
          loadQuoteQueue();
        });
      });
      list.querySelectorAll(".mpl-qq-open").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const row = await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(btn.dataset.id));
          current = row;
          showTab("tests");
          openEditor(row);
        });
      });
    } catch (e) {
      if (list) list.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  async function loadToday() {
    const list = document.getElementById("mpl-today-list");
    if (list) list.innerHTML = "<p class='muted'>Loading…</p>";
    try {
      const data = await api("/api/m3/micro-purchase-lab/todays-quote-work");
      if (!list) return;
      if (!(data.items || []).length) {
        list.innerHTML = "<p class='muted'>No quote work ready today.</p>";
        return;
      }
      list.innerHTML =
        "<ol class='mpl-today-ol'>" +
        data.items
          .map(
            (it, i) =>
              `<li><strong>${esc(it.supplier)}</strong> — P/N ${esc(it.part_number || "—")} — qty ${esc(it.quantity || "—")} · ${esc(it.quote_status)} · ${esc(it.next_action)}</li>`
          )
          .join("") +
        "</ol>";
    } catch (e) {
      if (list) list.innerHTML = `<p class="muted">${esc(e.message || e)}</p>`;
    }
  }

  function val(id) {
    const el = document.getElementById(id);
    return el ? el.value : "";
  }
  function set(id, v) {
    const el = document.getElementById(id);
    if (el) el.value = v == null ? "" : String(v);
  }
  function checked(id) {
    const el = document.getElementById(id);
    return !!(el && el.checked);
  }
  function setChecked(id, v) {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  }

  function openEditor(row) {
    const ed = document.getElementById("mpl-editor");
    if (ed) ed.hidden = false;
    current = row;
    set("mpl-f-solicitation", row.solicitation);
    set("mpl-f-source", row.source);
    set("mpl-f-agency", row.agency);
    set("mpl-f-product", row.product);
    set("mpl-f-manufacturer", row.manufacturer);
    set("mpl-f-part", row.part_number);
    set("mpl-f-nsn", row.nsn);
    set("mpl-f-qty", row.quantity);
    set("mpl-f-unit", row.government_unit || "EA");
    set("mpl-f-factor", row.commercial_units_per_gov_unit || 1);
    set("mpl-f-deadline", row.deadline);
    set("mpl-f-delivery", row.delivery_location);
    set("mpl-f-url", row.opportunity_url);
    set("mpl-f-size", row.estimated_opportunity_size);
    set("mpl-f-identity", row.identity_confidence || "UNKNOWN");
    set("mpl-f-min-profit", row.min_gross_profit_target ?? 0);
    set("mpl-f-min-margin", row.min_gross_margin_pct_target);
    set("mpl-f-freight", row.freight ?? 0);
    set("mpl-f-financing", row.financing_cost ?? 0);
    set("mpl-f-other", row.other_execution_costs ?? 0);
    set("mpl-f-candidate", row.candidate_bid_unit);
    set("mpl-f-notes", row.notes);
    const ha = (row.historical_awards || [])[0] || {};
    set("mpl-ha-date", ha.award_date);
    set("mpl-ha-price", ha.unit_price);
    set("mpl-ha-url", ha.source_url);
    set("mpl-ha-conf", ha.confidence || "MODERATE");
    const hm = (row.historical_market_costs || [])[0] || {};
    set("mpl-hm-date", hm.evidence_date);
    set("mpl-hm-price", hm.unit_price);
    set("mpl-hm-seller", hm.seller);
    set("mpl-hm-url", hm.url);
    const cm = (row.current_market_prices || [])[0] || {};
    set("mpl-cm-date", cm.date || cm.evidence_date);
    set("mpl-cm-price", cm.unit_price || cm.price);
    set("mpl-cm-seller", cm.seller);
    set("mpl-cm-url", cm.url);
    const sq = (row.supplier_quotes || [])[0] || {};
    set("mpl-sq-supplier", sq.supplier);
    set("mpl-sq-cost", sq.quoted_unit_cost);
    set("mpl-sq-freight", sq.freight ?? 0);
    set("mpl-sq-terms", sq.payment_terms || "UNKNOWN");
    set("mpl-sq-exp", sq.quote_expiration);
    setChecked("mpl-sq-pg", sq.personal_guarantee_required);
    setChecked("mpl-sq-pc", sq.personal_credit_required);
    setChecked("mpl-sq-prepay", sq.requires_full_prepayment);
    renderSummary(row);
    renderResearch(row);
  }

  function renderResearch(row) {
    const stagesEl = document.getElementById("mpl-research-stages");
    const sumEl = document.getElementById("mpl-research-summary");
    const ar = row.automated_research;
    if (!ar || !stagesEl || !sumEl) {
      if (stagesEl) stagesEl.hidden = true;
      if (sumEl) sumEl.hidden = true;
      return;
    }
    stagesEl.hidden = false;
    sumEl.hidden = false;
    const stages = ar.stages || {};
    const order = [
      "opportunity",
      "product_identity",
      "government_history",
      "historical_market",
      "current_market",
      "supplier_channels",
      "pricing_model",
      "quote_prep",
    ];
    stagesEl.innerHTML =
      "<h4>Research stages</h4><ul class='mpl-summary-list'>" +
      order
        .map((k) => {
          const s = stages[k] || {};
          return `<li><strong>${esc(k)}:</strong> ${esc(s.status || "NOT_RUN")} · ${esc(s.confidence || "UNKNOWN")}</li>`;
        })
        .join("") +
      "</ul>";
    const sm = ar.summary || {};
    const est = row.estimated_supplier_cost || {};
    sumEl.innerHTML = `<h4>Research Complete</h4>
      <ul class="mpl-summary-list">
        <li>Product identity: ${esc(sm.product_identity || "—")}</li>
        <li>Historical awards: ${esc(sm.historical_awards ?? 0)}</li>
        <li>Historical market observations: ${esc(sm.historical_market_observations ?? 0)}</li>
        <li>Current market observations: ${esc(sm.current_market_observations ?? 0)}</li>
        <li>Supplier candidates: ${esc(sm.supplier_candidates ?? 0)}</li>
        <li>Best historical-equivalent bid: ${esc(sm.best_historical_equivalent_bid ?? "—")}</li>
        <li>Current public market: ${esc(sm.current_public_market ?? "—")}</li>
        <li>Actual reseller quote: ${esc(sm.actual_reseller_quote || "NEEDED")}</li>
        <li>Next action: ${esc(ar.next_action || row.lab_next_action || "—")}</li>
        ${est.available ? `<li class="muted">${esc(est.label)} · ${esc(est.band_label)} · ${esc(est.estimation_label)}</li>` : ""}
      </ul>
      <p><strong>${esc(sm.recommended_next || "Review evidence, then Prepare Quotes")}</strong></p>`;
  }

  function renderSummary(row) {
    const st = document.getElementById("mpl-editor-status");
    if (st) st.textContent = "Status: " + (row.status || "—") + " · Class: " + (row.classification || "—");
    const box = document.getElementById("mpl-pricing-summary");
    const snap = row.economics_snapshot || {};
    const ps = row.pricing_summary || {};
    if (!box) return;
    const range = snap.economic_bid_range || {};
    box.innerHTML = `<h4>Pricing Analysis</h4>
      <ul class="mpl-summary-list">
        ${Object.entries(ps)
          .map(([k, v]) => `<li><strong>${esc(k)}:</strong> ${esc(v ?? "UNKNOWN")}</li>`)
          .join("")}
        <li><strong>Economic Bid Range:</strong> ${esc(range.low_unit ?? "—")} – ${esc(range.high_unit ?? "—")} /unit</li>
        <li class="muted">${esc(snap.label_historical_equivalent || "HISTORICAL-EQUIVALENT BID ESTIMATE")} — not a guaranteed win price</li>
      </ul>
      <p class="muted">Execution: ${esc(JSON.stringify((snap.execution || {}).reasons || []))}</p>`;
  }

  function collectPayload() {
    const haPrice = val("mpl-ha-price");
    const hmPrice = val("mpl-hm-price");
    const cmPrice = val("mpl-cm-price");
    const sqCost = val("mpl-sq-cost");
    const payload = {
      id: current && current.id,
      linked_opportunity_id: current && current.linked_opportunity_id,
      solicitation: val("mpl-f-solicitation"),
      source: val("mpl-f-source"),
      agency: val("mpl-f-agency"),
      product: val("mpl-f-product"),
      manufacturer: val("mpl-f-manufacturer"),
      part_number: val("mpl-f-part"),
      nsn: val("mpl-f-nsn"),
      quantity: val("mpl-f-qty"),
      government_unit: val("mpl-f-unit") || "EA",
      commercial_units_per_gov_unit: val("mpl-f-factor") || 1,
      deadline: val("mpl-f-deadline"),
      delivery_location: val("mpl-f-delivery"),
      opportunity_url: val("mpl-f-url"),
      estimated_opportunity_size: val("mpl-f-size"),
      identity_confidence: val("mpl-f-identity"),
      min_gross_profit_target: val("mpl-f-min-profit") || 0,
      min_gross_margin_pct_target: val("mpl-f-min-margin") || null,
      freight: val("mpl-f-freight") || 0,
      financing_cost: val("mpl-f-financing") || 0,
      other_execution_costs: val("mpl-f-other") || 0,
      candidate_bid_unit: val("mpl-f-candidate") || null,
      notes: val("mpl-f-notes"),
      opportunity_status: "OPEN",
      historical_awards: haPrice
        ? [
            {
              award_date: val("mpl-ha-date"),
              unit_price: haPrice,
              source_url: val("mpl-ha-url"),
              confidence: val("mpl-ha-conf"),
              source_type: "MANUAL",
            },
          ]
        : (current && current.historical_awards) || [],
      historical_market_costs: hmPrice
        ? [
            {
              evidence_date: val("mpl-hm-date"),
              unit_price: hmPrice,
              seller: val("mpl-hm-seller"),
              url: val("mpl-hm-url"),
              evidence_type: "OTHER",
              product_match: "EXACT",
              confidence: "MODERATE",
            },
          ]
        : (current && current.historical_market_costs) || [],
      current_market_prices: cmPrice
        ? [
            {
              date: val("mpl-cm-date"),
              unit_price: cmPrice,
              seller: val("mpl-cm-seller"),
              url: val("mpl-cm-url"),
              condition: "NEW_OEM",
              confidence: "MODERATE",
            },
          ]
        : (current && current.current_market_prices) || [],
      supplier_quotes: sqCost
        ? [
            {
              supplier: val("mpl-sq-supplier"),
              quoted_unit_cost: sqCost,
              freight: val("mpl-sq-freight") || 0,
              payment_terms: val("mpl-sq-terms"),
              quote_expiration: val("mpl-sq-exp"),
              personal_guarantee_required: checked("mpl-sq-pg"),
              personal_credit_required: checked("mpl-sq-pc"),
              requires_full_prepayment: checked("mpl-sq-prepay"),
              quote_date: new Date().toISOString().slice(0, 10),
            },
          ]
        : (current && current.supplier_quotes) || [],
      quote_status: sqCost ? "RECEIVED" : current && current.quote_status,
      automated_research: current && current.automated_research,
      research_completed_at: current && current.research_completed_at,
      supplier_candidates: current && current.supplier_candidates,
      recommended_quote_targets: current && current.recommended_quote_targets,
      quote_packets: current && current.quote_packets,
      quote_queue_items: current && current.quote_queue_items,
      recommended_historical_market: current && current.recommended_historical_market,
      recommended_current_market: current && current.recommended_current_market,
      estimated_supplier_cost: current && current.estimated_supplier_cost,
      research_pricing_model: current && current.research_pricing_model,
      lab_next_action: current && current.lab_next_action,
      identity_candidates: current && current.identity_candidates,
    };
    return payload;
  }

  async function saveCurrent() {
    const msg = document.getElementById("mpl-editor-msg");
    try {
      const payload = collectPayload();
      let row;
      if (payload.id) {
        row = await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(payload.id), {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      } else {
        row = await api("/api/m3/micro-purchase-lab/tests", { method: "POST", body: JSON.stringify(payload) });
      }
      current = row;
      openEditor(row);
      if (msg) msg.textContent = "Saved · status " + row.status;
      loadTests();
    } catch (e) {
      if (msg) msg.textContent = String(e.message || e);
    }
  }

  function blankTest() {
    current = {
      identity_confidence: "UNKNOWN",
      government_unit: "EA",
      commercial_units_per_gov_unit: 1,
      historical_awards: [],
      historical_market_costs: [],
      current_market_prices: [],
      supplier_quotes: [],
    };
    openEditor(current);
    showTab("tests");
  }

  function wire() {
    document.querySelectorAll(".mpl-tab").forEach((btn) => {
      btn.addEventListener("click", () => showTab(btn.dataset.mplTab));
    });
    document.getElementById("mpl-refresh-queue")?.addEventListener("click", loadQueue);
    document.getElementById("mpl-refresh-tests")?.addEventListener("click", loadTests);
    document.getElementById("mpl-refresh-quotes")?.addEventListener("click", loadQuoteQueue);
    document.getElementById("mpl-refresh-today")?.addEventListener("click", loadToday);
    document.getElementById("mpl-new-manual")?.addEventListener("click", blankTest);
    document.getElementById("mpl-save")?.addEventListener("click", saveCurrent);
    document.getElementById("mpl-research-batch")?.addEventListener("click", async () => {
      const msg = document.getElementById("mpl-queue-meta");
      if (msg) msg.textContent = "Running bounded research batch (max 5)…";
      try {
        const r = await api("/api/m3/micro-purchase-lab/research-queue", {
          method: "POST",
          body: JSON.stringify({ limit: 5 }),
        });
        if (msg) msg.textContent = "Batch done: " + JSON.stringify(r.results || r);
        loadTests();
      } catch (e) {
        if (msg) msg.textContent = String(e.message || e);
      }
    });
    document.getElementById("mpl-research")?.addEventListener("click", async () => {
      const msg = document.getElementById("mpl-editor-msg");
      try {
        if (!current || !current.id) await saveCurrent();
        if (!current || !current.id) return;
        if (msg) msg.textContent = "Researching…";
        const row = await api(
          "/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(current.id) + "/research",
          { method: "POST", body: JSON.stringify({ allow_paid_research: false }) }
        );
        current = row;
        openEditor(row);
        if (msg) msg.textContent = "Research complete · status " + row.status + " · next " + (row.lab_next_action || "");
      } catch (e) {
        if (msg) msg.textContent = String(e.message || e);
      }
    });
    document.getElementById("mpl-prepare-quotes")?.addEventListener("click", async () => {
      const msg = document.getElementById("mpl-editor-msg");
      try {
        if (!current || !current.id) await saveCurrent();
        if (!current || !current.id) return;
        const row = await api(
          "/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(current.id) + "/prepare-quotes",
          { method: "POST", body: JSON.stringify({ top_n: 4 }) }
        );
        current = row;
        openEditor(row);
        if (msg) msg.textContent = "Quotes prepared · " + ((row.quote_queue_items || []).length) + " queue items";
        showTab("quotes");
      } catch (e) {
        if (msg) msg.textContent = String(e.message || e);
      }
    });
    document.getElementById("mpl-load-opp")?.addEventListener("click", async () => {
      const q = window.prompt("Search opportunity (title / solicitation / id):");
      if (!q) return;
      const data = await api("/api/m3/micro-purchase-lab/search-opportunities?q=" + encodeURIComponent(q));
      const first = (data.opportunities || [])[0];
      if (!first) {
        alert("No matches");
        return;
      }
      const row = await api("/api/m3/micro-purchase-lab/load-opportunity", {
        method: "POST",
        body: JSON.stringify({ canonical_id: first.canonical_id }),
      });
      current = row;
      openEditor(row);
    });
    document.getElementById("mpl-copy-quote")?.addEventListener("click", async () => {
      if (!current || !current.id) {
        await saveCurrent();
      }
      if (!current || !current.id) return;
      const packets = current.quote_packets || [];
      let text;
      if (packets.length) {
        text = packets.map((p) => "=== " + (p.supplier || "Supplier") + " ===\n" + (p.quote_request_text || "")).join("\n\n");
      } else {
        const q = await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(current.id) + "/quote-request");
        text = q.text;
      }
      const pre = document.getElementById("mpl-quote-text");
      if (pre) {
        pre.hidden = false;
        pre.textContent = text;
      }
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {}
    });
    document.getElementById("mpl-duplicate")?.addEventListener("click", async () => {
      if (!current || !current.id) return;
      current = await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(current.id) + "/duplicate", {
        method: "POST",
        body: "{}",
      });
      openEditor(current);
      loadTests();
    });
    document.getElementById("mpl-archive")?.addEventListener("click", async () => {
      if (!current || !current.id) return;
      await api("/api/m3/micro-purchase-lab/tests/" + encodeURIComponent(current.id) + "/archive", {
        method: "POST",
        body: "{}",
      });
      document.getElementById("mpl-editor").hidden = true;
      current = null;
      loadTests();
    });
  }

  window.MicroPurchaseLab = {
    open() {
      showTab("queue");
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
