/** Deal Workspace — operator-first commercial CRM (GET on load; no paid AI). */
(function () {
  const view = document.getElementById("view-deal-workspace");
  if (!view) return;

  let currentDealId = null;
  let currentWs = null;
  const SECTIONS = [
    "OVERVIEW",
    "SUPPLIERS",
    "QUOTES",
    "FINANCING",
    "CO CLARIFICATION",
    "ECONOMICS",
    "TODAY",
    "MISSING INFO",
    "DOCUMENTS",
    "BOM",
    "DEAL / BID",
    "CALL TRANSCRIPTS",
    "ASK ABOUT THIS DEAL",
  ];

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hideOthers() {
    [
      "view-dashboard",
      "view-contract-detail",
      "view-performance",
      "view-settings",
      "view-help",
      "view-product",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    });
    document.querySelectorAll(".main-nav .tab").forEach((t) => t.classList.remove("active"));
    const tab = document.getElementById("tab-product");
    if (tab) tab.classList.add("active");
    view.hidden = false;
  }

  function statusStrip(ws) {
    const na = ws.next_action || {};
    const deal = ws.deal_readiness || {};
    const bid = ws.bid_readiness || {};
    const urg = ws.deadline_urgency || {};
    const cs = ws.commercial_status || {};
    const warns = (ws.warnings || []).filter((w) => w.status === "ACTIVE" && w.severity === "CRITICAL");
    const warnHtml = warns
      .slice(0, 3)
      .map(
        (w) =>
          `<div class="card" style="margin-bottom:0.75rem;border-left:4px solid #8a1f1f;">` +
          `<div style="font-size:0.75rem;">⚠ ${escapeHtml(w.warning_type || "WARNING")}</div>` +
          `<strong>${escapeHtml(w.message || "")}</strong>` +
          `<p><em>WHY IT MATTERS:</em> ${escapeHtml(w.why_it_matters || "")}</p>` +
          `<p><em>NEXT ACTION:</em> ${escapeHtml(w.recommended_next_action || "")}</p></div>`
      )
      .join("");
    return (
      warnHtml +
      `<div class="card" style="margin-bottom:1rem;border-left:4px solid #1a5f4a;">` +
      `<div style="font-size:0.75rem;letter-spacing:0.04em;">NEXT ACTION</div>` +
      `<strong style="font-size:1.2rem;">${escapeHtml(na.action || "Review workspace")}</strong>` +
      `<p style="margin:0.35rem 0 0;">${escapeHtml(na.why || "")}</p></div>` +
      `<div class="card" style="margin-bottom:1rem;display:grid;gap:0.5rem;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));">` +
      `<div><div style="font-size:0.7rem;">DEADLINE</div><strong>${escapeHtml(
        urg.close_label || (ws.opportunity || {}).due_date || "?"
      )}</strong>` +
      (urg.urgent
        ? `<div style="color:#8a1f1f;">~${escapeHtml(String(urg.hours_remaining_approx))}h left</div>`
        : "") +
      `</div>` +
      `<div><div style="font-size:0.7rem;">DEAL STATUS</div><strong>${escapeHtml(
        deal.status || "?"
      )}</strong></div>` +
      `<div><div style="font-size:0.7rem;">BID STATUS</div><strong>${escapeHtml(
        bid.status || "?"
      )}</strong></div>` +
      `<div><div style="font-size:0.7rem;">COMMERCIAL</div><strong>${escapeHtml(
        cs.label || "?"
      )}</strong></div>` +
      `</div>`
    );
  }

  async function supplierAction(supplierId, type) {
    if (!currentDealId) return;
    let payload = {
      supplier_id: supplierId,
      activity_type: type,
      what_happened: type,
    };
    if (type === "NOTE" || type === "ADD_NOTE") {
      const text = window.prompt("Note?") || "";
      if (!text) return;
      await fetch(`/api/deals/${currentDealId}/notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entity_type: "supplier", entity_id: supplierId, text }),
      });
      return loadDeal(currentDealId);
    }
    if (type === "FOLLOWUP" || type === "SET_FOLLOWUP") {
      payload.activity_type = "FOLLOWUP";
      payload.next_action = window.prompt("Next action?") || "Follow up";
      payload.next_action_at = window.prompt("Follow-up date YYYY-MM-DD?") || "";
    }
    const res = await fetch(`/api/deals/${currentDealId}/supplier-action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    if (res.offer_quote_entry) {
      alert("Quote received logged — open QUOTES tab to enter quote details.");
    }
    return loadDeal(currentDealId);
  }

  async function submitQuoteForm(ev) {
    ev.preventDefault();
    if (!currentDealId) return;
    const fd = new FormData(ev.target);
    const body = {};
    fd.forEach((v, k) => {
      if (v === "" || v === "UNKNOWN") body[k] = v === "UNKNOWN" ? "UNKNOWN" : null;
      else if (["quantity", "unit_price", "extended_product_price", "freight", "other_required_charges", "total", "supplier_id", "memory_modules_per_server_quoted", "storage_drives_per_server_quoted"].includes(k)) {
        body[k] = v === "UNKNOWN" ? "UNKNOWN" : Number(v);
      } else if (["delivery_confirmed", "oem_letter_available", "federal_channel_confirmed", "upfront_payment_required", "bom_match"].includes(k)) {
        body[k] = v === "true" ? true : v === "false" ? false : null;
      } else body[k] = v;
    });
    // Explicit UNKNOWN for blank freight — never coerce to 0
    if (body.freight === null || body.freight === undefined || body.freight === "") body.freight = "UNKNOWN";
    await fetch(`/api/deals/${currentDealId}/quotes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return loadDeal(currentDealId);
  }

  function quoteEntryForm(ws) {
    const supOpts = (ws.suppliers || [])
      .map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`)
      .join("");
    return (
      `<form id="quote-entry-form" class="card" style="margin-top:1rem;">` +
      `<h4>Enter supplier quote</h4>` +
      `<p style="font-size:0.85rem;">Leave blank or UNKNOWN when unknown. Blank freight is never treated as $0.</p>` +
      `<label>Supplier <select name="supplier_id">${supOpts}</select></label>` +
      `<label>Quote # <input name="quote_number" /></label>` +
      `<label>Quote date <input name="quote_date" placeholder="YYYY-MM-DD" /></label>` +
      `<label>Expires <input name="expires" placeholder="YYYY-MM-DD" /></label>` +
      `<label>Quantity <input name="quantity" placeholder="14" /></label>` +
      `<label>Unit price <input name="unit_price" /></label>` +
      `<label>Extended product price <input name="extended_product_price" /></label>` +
      `<label>Freight <input name="freight" placeholder="UNKNOWN" /></label>` +
      `<label>Other required charges <input name="other_required_charges" placeholder="UNKNOWN" /></label>` +
      `<label>Total <input name="total" placeholder="auto if known" /></label>` +
      `<label>Payment terms <input name="payment_terms" /></label>` +
      `<label>Availability <input name="availability" /></label>` +
      `<label>Lead time <input name="lead_time" /></label>` +
      `<label>Delivery confirmed? <select name="delivery_confirmed"><option value="">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
      `<label>OEM letter available? <select name="oem_letter_available"><option value="">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
      `<label>Federal channel confirmed? <select name="federal_channel_confirmed"><option value="">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
      `<label>Upfront payment required? <select name="upfront_payment_required"><option value="">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
      `<label>BOM match? <select name="bom_match"><option value="">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
      `<label>Memory modules/server quoted <input name="memory_modules_per_server_quoted" placeholder="8" /></label>` +
      `<label>Storage drives/server quoted <input name="storage_drives_per_server_quoted" placeholder="6" /></label>` +
      `<label>Notes / document <textarea name="document_notes"></textarea></label>` +
      `<button type="submit" class="btn btn-primary">Save quote</button></form>`
    );
  }

  function render(ws) {
    currentWs = ws;
    const idEl = document.getElementById("deal-workspace-id");
    const readyEl = document.getElementById("deal-workspace-readiness");
    const tabsEl = document.getElementById("deal-workspace-tabs");
    const panelsEl = document.getElementById("deal-workspace-panels");
    if (!tabsEl || !panelsEl) return;

    const opp = ws.opportunity || {};
    if (idEl) idEl.textContent = `Opp ${opp.id || "?"} — ${opp.notice_id || ""}`;

    if (readyEl) {
      readyEl.innerHTML =
        statusStrip(ws) +
        `<p style="font-size:0.8rem;">LIVE_API_REQUESTS: ${ws.LIVE_API_REQUESTS ?? 0} | external all zero on load</p>`;
    }

    const panels = {
      OVERVIEW: () => {
        const plan = ((ws.commercial_plan || {}).steps || [])
          .map((s) => `<li>${s.id}. ${escapeHtml(s.action)} — <em>${escapeHtml(s.status)}</em></li>`)
          .join("");
        return (
          `<p><strong>${escapeHtml(opp.title || "")}</strong></p>` +
          `<p>Agency: ${escapeHtml(opp.agency || "")}</p>` +
          `<h4>Commercial execution plan</h4><ol style="list-style:none;padding:0;">${plan}</ol>` +
          `<details><summary>RFQ packet (advanced)</summary><pre style="white-space:pre-wrap;font-size:0.75rem;">${escapeHtml(
            JSON.stringify(ws.supplier_rfq_packet || {}, null, 2)
          )}</pre></details>`
        );
      },
      SUPPLIERS: () => {
        const sheets = ws.supplier_call_sheets || [];
        const script = ws.supplier_call_script || {};
        const playbook = ws.negotiation_playbook || {};
        const cards = (ws.suppliers || [])
          .map((s) => {
            const sheet = sheets.find((x) => x.supplier_id === s.id) || sheets.find((x) => x.supplier === s.name) || {};
            const contact =
              sheet.contact_status === "CONTACT_INFORMATION_NEEDED" || !sheet.contact
                ? "CONTACT INFORMATION NEEDED"
                : `${sheet.contact.name || ""} ${sheet.contact.phone || ""}`;
            return (
              `<div class="card" style="margin-bottom:1rem;" data-supplier="${s.id}">` +
              `<strong>${escapeHtml(s.name)}</strong>` +
              `<p>Contact: ${escapeHtml(contact)}</p>` +
              `<p>Why: ${escapeHtml(sheet.why_we_are_calling || "Obtain firm quote")}</p>` +
              `<div style="display:flex;flex-wrap:wrap;gap:0.35rem;margin:0.5rem 0;">` +
              ["CALLED", "LEFT_VOICEMAIL", "SPOKE_WITH_CONTACT", "QUOTE_REQUESTED", "QUOTE_RECEIVED"]
                .map(
                  (t) =>
                    `<button type="button" class="btn btn-secondary-action" data-sa="${t}" data-sid="${s.id}">${t.replace(
                      /_/g,
                      " "
                    )}</button>`
                )
                .join("") +
              `<button type="button" class="btn btn-secondary-action" data-sa="NOTE" data-sid="${s.id}">Add Note</button>` +
              `<button type="button" class="btn btn-secondary-action" data-sa="FOLLOWUP" data-sid="${s.id}">Set Follow-Up</button>` +
              `</div></div>`
            );
          })
          .join("");
        return (
          cards +
          `<details><summary>Call script</summary><p><em>${escapeHtml(script.opening || "")}</em></p>` +
          `<ul>${(script.sections || [])
            .map((sec) => `<li><strong>${escapeHtml(sec.topic)}</strong>: ${escapeHtml(sec.prompt)}</li>`)
            .join("")}</ul>` +
          `<p style="color:#8a1f1f;">${escapeHtml((script.prohibited_claims || []).join(" "))}</p></details>` +
          `<details><summary>Negotiation playbook (suggestions, not facts)</summary><ul>${(
            playbook.strategies || []
          )
            .map((st) => `<li>${escapeHtml(st.strategy)} — ${escapeHtml(st.why_it_may_help)}</li>`)
            .join("")}</ul></details>`
        );
      },
      QUOTES: () => {
        const qs = ws.quotes || [];
        const cmp = ws.quote_comparison || {};
        const lowest = cmp.lowest_verified_compliant_acquisition_cost;
        const rows = qs
          .map((q) => {
            const v = q.validation || {};
            return (
              `<li>#${q.id} ${escapeHtml(q.supplier_name || "")} — unit=${q.unit_price ?? "UNKNOWN"} ` +
              `validation=${escapeHtml(v.status || q.validation_status || "")}` +
              (v.display ? `<pre style="white-space:pre-wrap;">${escapeHtml(v.display)}</pre>` : "") +
              `</li>`
            );
          })
          .join("");
        return (
          (lowest
            ? `<p><strong>LOWEST VERIFIED COMPLIANT ACQUISITION COST:</strong> ${escapeHtml(
                String(lowest.supplier)
              )} @ ${escapeHtml(String(lowest.total_acquisition_cost))}</p>`
            : `<p>No comparable valid quotes yet — incomplete quotes are never called cheapest.</p>`) +
          `<ul>${rows || "<li>No quotes entered</li>"}</ul>` +
          quoteEntryForm(ws)
        );
      },
      FINANCING: () => {
        const pkt = ws.financing_packet || {};
        return (
          `<div class="card"><strong>Financing verification packet</strong>` +
          `<p>Hard requirements: NO PG / NO personal credit / $0 cash</p>` +
          `<ul>${(pkt.questions || []).map((q) => `<li>${escapeHtml(q)}</li>`).join("")}</ul></div>` +
          `<pre>${escapeHtml(JSON.stringify(ws.financing || {}, null, 2))}</pre>` +
          `<form id="fin-entry-form" class="card"><h4>Record financing call result</h4>` +
          `<label>Provider <input name="provider" /></label>` +
          `<label>Contact <input name="contact" /></label>` +
          `<label>Transaction eligible? <select name="transaction_eligible"><option value="UNKNOWN">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
          `<label>PG required? <select name="pg_required"><option value="UNKNOWN">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
          `<label>Personal credit? <select name="personal_credit_required"><option value="UNKNOWN">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
          `<label>Cash contribution <input name="cash_contribution" placeholder="UNKNOWN or 0" /></label>` +
          `<label>Supplier paid direct? <select name="supplier_paid_direct"><option value="UNKNOWN">UNKNOWN</option><option value="true">Yes</option><option value="false">No</option></select></label>` +
          `<label>Notes <textarea name="notes"></textarea></label>` +
          `<button type="submit" class="btn btn-primary">Save financing</button></form>`
        );
      },
      "CO CLARIFICATION": () => {
        const card = ws.co_clarification_card || {};
        return (
          `<div class="card">` +
          `<div style="font-size:0.75rem;">CO QUESTION READY</div>` +
          `<strong>${escapeHtml(card.topic || "FOB / freight")}</strong>` +
          `<p>Status: ${escapeHtml(card.status || (card.timing || {}).status || "?")}</p>` +
          `<p>Reason: ${escapeHtml((card.timing || {}).reason || "")}</p>` +
          `<p>Deadline risk: ${escapeHtml(JSON.stringify((card.timing || {}).deadline_risk || []))}</p>` +
          `<p><em>No auto-email. Operator may review/send manually.</em></p>` +
          `<details><summary>Draft</summary><pre style="white-space:pre-wrap;">${escapeHtml(
            card.draft || ""
          )}</pre></details></div>`
        );
      },
      ECONOMICS: () => {
        const e = ws.economics || {};
        const pb = ws.proposed_bid || e.proposed_bid_price || {};
        return (
          `<div class="card">` +
          `<p>PROPOSED BID PRICE: ${escapeHtml(String(pb.amount ?? pb.value ?? "UNKNOWN"))} ` +
          `<em>(COMPANY_PROPOSED — not government value)</em></p>` +
          `<p>TOTAL VERIFIED/CALCULATED REQUIRED COST: ${escapeHtml(
            String(e.total_verified_calculated_required_cost ?? "INCOMPLETE")
          )}</p>` +
          `<p>ACTUAL PROFIT: ${escapeHtml(String(e.actual_profit ?? "INCOMPLETE"))}</p>` +
          `<p>ACTUAL MARGIN: ${escapeHtml(String(e.actual_margin ?? "INCOMPLETE"))}</p>` +
          `<p>20% GROSS RETENTION TARGET: ${escapeHtml(
            String((e.gross_retention_20pct_target || {}).value ?? "n/a")
          )} <em>POLICY only — not a contract cost</em></p>` +
          `<p>FINANCING STATUS: ${escapeHtml(String(e.financing_status || (ws.financing || {}).status))}</p>` +
          `</div>` +
          `<form id="bid-form" class="card"><h4>Set company proposed bid</h4>` +
          `<label>Amount <input name="amount" type="number" step="0.01" /></label>` +
          `<label>Reason <input name="reason" /></label>` +
          `<button type="submit" class="btn btn-primary">Save proposed bid</button></form>`
        );
      },
      TODAY: () => {
        const tq = ws.today_queue || {};
        return (
          `<h3>${escapeHtml(tq.title || "TODAY")}</h3><ul>` +
          ((tq.actions || [])
            .map((a) => `<li><strong>P${a.priority}</strong> ${escapeHtml(a.action)}</li>`)
            .join("") || "<li>Nothing due</li>") +
          `</ul>`
        );
      },
      "MISSING INFO": () => {
        return (ws.missing_info || [])
          .map(
            (m) =>
              `<div class="card"><strong>${escapeHtml(m.description || m.fact_key)}</strong>` +
              `<p>${escapeHtml(m.status)} | Safe to ask CO: ${m.safe_to_ask_co ? "YES" : "NO"}</p></div>`
          )
          .join("");
      },
      DOCUMENTS: () =>
        `<ul>${(ws.documents || [])
          .map((d) => `<li>${escapeHtml(d.document_type)} — ${escapeHtml(d.filename || "")}</li>`)
          .join("")}</ul>`,
      BOM: () => {
        const rows = (ws.bom || [])
          .map(
            (b) =>
              `<tr><td>${escapeHtml(b.component)}</td><td>${escapeHtml(String(b.value))}</td>` +
              `<td>${escapeHtml(String(b.quantity))}</td><td>${escapeHtml(b.status)}</td></tr>`
          )
          .join("");
        return (
          `<p>Gate: ${escapeHtml((ws.bom_gate || {}).status || "")}</p>` +
          `<table><thead><tr><th>Component</th><th>Value</th><th>Qty</th><th>Status</th></tr></thead>` +
          `<tbody>${rows}</tbody></table>`
        );
      },
      "DEAL / BID": () => {
        const deal = ws.deal_readiness || {};
        const bid = ws.bid_readiness || {};
        return (
          `<div class="card"><strong>DEAL: ${escapeHtml(deal.status)}</strong><ul>${(
            deal.blockers || []
          )
            .map((b) => `<li>${escapeHtml(b)}</li>`)
            .join("")}</ul></div>` +
          `<div class="card"><strong>BID: ${escapeHtml(bid.status)}</strong><ul>${(bid.blockers || [])
            .map((b) => `<li>${escapeHtml(b)}</li>`)
            .join("")}</ul></div>`
        );
      },
      "CALL TRANSCRIPTS": () => {
        const hist = (ws.transcripts || [])
          .map((t) => {
            const a = t.analysis || {};
            return (
              `<div class="card" style="margin-bottom:0.75rem;">` +
              `<strong>${escapeHtml(t.transcript_type)} · ${escapeHtml(t.organization_name || "")}</strong>` +
              `<p>Status: ${escapeHtml(t.analysis_status || "NOT_ANALYZED")}</p>` +
              `<p>${escapeHtml(t.raw_transcript_preview || "")}</p>` +
              (a.status === "NOT_ANALYZED"
                ? `<p><em>${escapeHtml(a.ui_message || "ANALYSIS_AVAILABLE_LATER")}</em></p>`
                : `<details><summary>What we learned</summary><pre>${escapeHtml(
                    JSON.stringify(a, null, 2)
                  )}</pre></details>`) +
              `</div>`
            );
          })
          .join("");
        return (
          `<form id="transcript-paste-form" class="card">` +
          `<h4>PASTE CALL TRANSCRIPT</h4>` +
          `<label>Type <select name="transcript_type">` +
          `<option value="SUPPLIER">Supplier</option>` +
          `<option value="FINANCIER">Financier</option>` +
          `<option value="CONTRACTING_OFFICER">Contracting Officer</option>` +
          `<option value="OTHER">Other</option></select></label>` +
          `<label>Organization <input name="organization_name" /></label>` +
          `<label>Transcript <textarea name="raw_transcript" rows="8" required></textarea></label>` +
          `<button type="submit" class="btn btn-primary">Save transcript</button></form>` +
          `<h4>Transcript history</h4>${hist || "<p>No transcripts yet.</p>"}` +
          `<button type="button" class="btn btn-secondary-action" id="show-ask-next">SHOW ME WHAT TO ASK NEXT</button>` +
          `<p id="ask-next-msg" style="font-size:0.85rem;"></p>`
        );
      },
      "ASK ABOUT THIS DEAL": () => {
        return (
          `<div class="card">` +
          `<h4>Ask About This Deal</h4>` +
          `<p style="font-size:0.85rem;">Preview only — no AI calls in this build.</p>` +
          `<form id="ask-deal-form">` +
          `<label>Question <input name="question" placeholder="What am I missing?" style="width:100%;" /></label>` +
          `<button type="submit" class="btn btn-primary">Preview context</button></form>` +
          `<pre id="ask-deal-preview" style="white-space:pre-wrap;font-size:0.75rem;margin-top:1rem;"></pre>` +
          `</div>`
        );
      },
    };

    tabsEl.innerHTML = SECTIONS.map(
      (s, i) =>
        `<button type="button" class="contract-detail-tab${i === 0 ? " active" : ""}" data-section="${escapeHtml(
          s
        )}">${escapeHtml(s)}</button>`
    ).join("");
    panelsEl.innerHTML = `<div class="deal-ws-panel">${panels.OVERVIEW()}</div>`;

    function bindPanel() {
      panelsEl.querySelectorAll("[data-sa]").forEach((btn) => {
        btn.addEventListener("click", () =>
          supplierAction(Number(btn.getAttribute("data-sid")), btn.getAttribute("data-sa"))
        );
      });
      const qf = document.getElementById("quote-entry-form");
      if (qf) qf.addEventListener("submit", submitQuoteForm);
      const ff = document.getElementById("fin-entry-form");
      if (ff) {
        ff.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const fd = new FormData(ff);
          const body = {};
          fd.forEach((v, k) => {
            body[k] = v === "" ? "UNKNOWN" : v === "true" ? true : v === "false" ? false : v;
          });
          await fetch(`/api/deals/${currentDealId}/financing-entry`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          loadDeal(currentDealId);
        });
      }
      const bf = document.getElementById("bid-form");
      if (bf) {
        bf.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const fd = new FormData(bf);
          await fetch(`/api/deals/${currentDealId}/proposed-bid`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              amount: fd.get("amount") ? Number(fd.get("amount")) : null,
              reason: fd.get("reason") || null,
            }),
          });
          loadDeal(currentDealId);
        });
      }
      const tf = document.getElementById("transcript-paste-form");
      if (tf) {
        tf.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const fd = new FormData(tf);
          await fetch(`/api/deals/${currentDealId}/transcripts`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              raw_transcript: fd.get("raw_transcript"),
              transcript_type: fd.get("transcript_type"),
              organization_name: fd.get("organization_name") || null,
            }),
          });
          loadDeal(currentDealId);
        });
      }
      const askNext = document.getElementById("show-ask-next");
      if (askNext) {
        askNext.addEventListener("click", () => {
          const msg = document.getElementById("ask-next-msg");
          if (msg) msg.textContent = "NOT_ANALYZED — suggested questions available after future transcript analysis.";
        });
      }
      const af = document.getElementById("ask-deal-form");
      if (af) {
        af.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const fd = new FormData(af);
          const q = fd.get("question") || "What am I missing?";
          const data = await fetch(`/api/deals/${currentDealId}/ask-about-deal`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: q }),
          }).then((r) => r.json());
          const pre = document.getElementById("ask-deal-preview");
          if (pre) pre.textContent = JSON.stringify(data.context_preview || data, null, 2);
        });
      }
    }
    bindPanel();

    tabsEl.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        tabsEl.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const key = btn.getAttribute("data-section");
        const fn = panels[key];
        panelsEl.innerHTML = `<div class="deal-ws-panel">${fn ? fn() : ""}</div>`;
        bindPanel();
      });
    });
  }

  async function loadDeal(dealId) {
    currentDealId = dealId;
    hideOthers();
    const readyEl = document.getElementById("deal-workspace-readiness");
    if (readyEl) readyEl.textContent = "Loading deal workspace…";
    try {
      // Reconcile only when operator explicitly triggers via Product Pipeline (not auto on load)
      const ws = await fetch(`/api/deals/${dealId}`).then((r) => r.json());
      if (ws.detail) throw new Error(ws.detail);
      render(ws);
    } catch (err) {
      if (readyEl) readyEl.textContent = "Failed to load deal workspace (no paid retry).";
    }
  }

  window.openDealWorkspace = function (dealId) {
    loadDeal(dealId);
  };

  const back = document.getElementById("deal-workspace-back");
  if (back) {
    back.addEventListener("click", () => {
      view.hidden = true;
      const tab = document.getElementById("tab-product");
      if (tab) tab.click();
    });
  }
  const refresh = document.getElementById("deal-workspace-refresh");
  if (refresh) {
    refresh.addEventListener("click", () => {
      if (currentDealId) loadDeal(currentDealId);
    });
  }

  window.addEventListener("hashchange", () => {
    const m = String(location.hash || "").match(/^#deal-(\d+)$/);
    if (m) loadDeal(Number(m[1]));
  });
  const m0 = String(location.hash || "").match(/^#deal-(\d+)$/);
  if (m0) loadDeal(Number(m0[1]));
})();
