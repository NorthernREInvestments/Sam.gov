/** Two-tier pricing intelligence UI, bid calculator, and dashboard. */

function formatFrequencyLabel(freq, pendingMessage) {
  if (freq == null) return pendingMessage ? "Pending PDF read" : "Not found in PWS";
  const n = Number(freq);
  if (n === 7) return "Daily (7 days/week)";
  if (n === 5) return "5 days per week";
  if (n === 1) return "Weekly";
  return `${n} days per week`;
}

function formatSqFtDisplay(sqft, pendingMessage) {
  if (!sqft) return pendingMessage ? "Pending PDF read" : "Not found in PWS";
  return `${Number(sqft).toLocaleString()} sq ft`;
}

function renderPwsSection(pws) {
  const p = pws || {};
  const pending = p.status === "pending_piee" || p.status === "pending_pdfs";
  const pendingNote = pending && p.message
    ? `<p class="pricing-note pricing-note-warn">${escapeHtml(p.message)}</p>`
    : "";
  const pendingMsg = pending ? p.message : null;
  const specials = (p.special_requirements || []).length
    ? `<p class="pricing-note">Special requirements: ${escapeHtml(p.special_requirements.join(", "))}</p>`
    : "";
  const wd =
    p.wage_determination_number || p.wage_determination_rate
      ? `<p class="pricing-note">Wage determination: ${escapeHtml(p.wage_determination_number || "—")}${
          p.wage_determination_rate ? ` · $${p.wage_determination_rate}/hr` : ""
        }</p>`
      : `<p class="pricing-note">Wage determination: ${pending ? "Pending PDF read" : "Not found in PWS"}</p>`;
  return `
    <div class="pricing-tier pricing-tier-pws">
      <h4 class="pricing-tier-title">Contract scope (from PWS)</h4>
      ${pendingNote}
      <div class="pricing-stats">
        <div class="pricing-stat">
          <span class="pricing-stat-label">Square footage</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(formatSqFtDisplay(p.square_footage, pendingMsg))}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Cleaning frequency</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(formatFrequencyLabel(p.cleaning_frequency_per_week, pendingMsg))}</span>
        </div>
        ${p.building_type ? `<div class="pricing-stat"><span class="pricing-stat-label">Building type</span><span class="pricing-stat-value pricing-stat-text">${escapeHtml(p.building_type)}</span></div>` : ""}
      </div>
      ${wd}
      ${specials}
    </div>`;
}

function renderInternalPricingSection(internal) {
  const i = internal || {};
  if (!i.available) {
    return `
      <div class="pricing-tier pricing-tier-internal">
        <h4 class="pricing-tier-title">Your internal pricing</h4>
        <p class="pricing-note pricing-note-muted">${escapeHtml(i.message || "Not enough internal data yet.")}</p>
      </div>`;
  }
  return `
    <div class="pricing-tier pricing-tier-internal">
      <h4 class="pricing-tier-title">Your internal pricing</h4>
      <p class="pricing-intro">${escapeHtml(i.confidence_label || "")} · Based on ${i.match_count} similar contracts (${escapeHtml(i.match_description || "")})</p>
      <div class="pricing-stats">
        <div class="pricing-stat pricing-stat-highlight">
          <span class="pricing-stat-label">Avg $/sq ft/visit</span>
          <span class="pricing-stat-value">${escapeHtml(formatUnitRate(i.avg_price_per_sqft_per_visit))}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Recommended bid range</span>
          <span class="pricing-stat-value">${formatMoney(i.recommended_bid_low)} – ${formatMoney(i.recommended_bid_high)}</span>
        </div>
      </div>
      ${i.recommended_annual_bid ? `<div class="pricing-bid-hero"><span class="pricing-bid-label">Midpoint bid</span><span class="pricing-bid-range">${formatMoney(i.recommended_annual_bid)}</span></div>` : ""}
      ${i.formula_note ? `<p class="pricing-note pricing-formula">${escapeHtml(i.formula_note)}</p>` : ""}
      ${renderMatchedContractsTable(i.matched_contracts)}
    </div>`;
}

function renderMatchedContractsTable(rows) {
  if (!rows?.length) return "";
  return `
    <table class="pricing-table pricing-table-compact">
      <thead><tr><th>Contract</th><th>Status</th><th>$/sq ft/visit</th><th>Note</th></tr></thead>
      <tbody>
        ${rows
          .map((row) => {
            const rowClass = row.location_note?.includes("expired") ? "pricing-row-same-site" : "";
            return `<tr class="${rowClass}">
              <td>${escapeHtml(row.title || row.notice_id || "—")}</td>
              <td>${escapeHtml(row.status || "—")}</td>
              <td>${row.price_per_sqft_per_visit != null ? escapeHtml(formatUnitRate(row.price_per_sqft_per_visit)) : "—"}</td>
              <td>${row.location_note ? `<span class="pricing-location-badge">${escapeHtml(row.location_note)}</span>` : "—"}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function renderSiteHistorySection(siteHistory) {
  if (!siteHistory?.length) return "";
  return `
    <div class="pricing-tier pricing-tier-site-history">
      <h4 class="pricing-tier-title">Same address & scope — prior contracts</h4>
      <p class="pricing-note">Prior solicitation(s) at this exact address with the same scope of work (matching NAICS). Solicitation numbers may change on recompetes.</p>
      <table class="pricing-table pricing-table-compact">
        <thead><tr><th>Contract</th><th>Due date</th><th>Status</th><th>Winning bid</th><th>Note</th></tr></thead>
        <tbody>
          ${siteHistory
            .map(
              (row) => `<tr class="pricing-row-same-site">
            <td>${escapeHtml(row.title || row.notice_id || "—")}</td>
            <td>${escapeHtml(row.due_date || "—")}</td>
            <td>${escapeHtml(row.status || "—")}</td>
            <td>${row.awarded_amount != null ? formatMoney(row.awarded_amount) : "—"}</td>
            <td><span class="pricing-location-badge">${escapeHtml(row.location_note || "Same address & scope")}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
    </div>`;
}

function renderRegionalBenchmarkSection(regional) {
  const r = regional || {};
  if (r.error) {
    return `<div class="pricing-tier"><p class="pricing-panel-error">${escapeHtml(r.error)}</p></div>`;
  }
  const state = r.state_name || r.state_code || "this state";
  return `
    <div class="pricing-tier pricing-tier-regional">
      <h4 class="pricing-tier-title">Regional award benchmarks</h4>
      <p class="pricing-note">${escapeHtml(r.benchmark_note || `Based on similar contracts in ${state}.`)}</p>
      <div class="pricing-stats">
        <div class="pricing-stat">
          <span class="pricing-stat-label">Regional average</span>
          <span class="pricing-stat-value">${formatMoney(r.average_annual_award)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Award range</span>
          <span class="pricing-stat-value">${formatMoney(r.lowest_award)} – ${formatMoney(r.highest_award)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Contracts found</span>
          <span class="pricing-stat-value">${r.awards_count ?? 0}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Confidence</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(r.confidence_label || "—")}</span>
        </div>
        <div class="pricing-stat pricing-stat-wide">
          <span class="pricing-stat-label">Most frequent winner</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(r.most_frequent_winner || "—")}</span>
        </div>
      </div>
      ${renderRegionalAwardsTable(r.awards)}
    </div>`;
}

function renderRegionalAwardsTable(awards) {
  if (!awards?.length) return "";
  return `
    <table class="pricing-table pricing-table-compact">
      <thead><tr><th>Date</th><th>Recipient</th><th>Amount</th><th>Location</th><th>Agency</th></tr></thead>
      <tbody>
        ${awards
          .slice(0, 10)
          .map((a) => {
            const rowClass = a.location_priority ? "pricing-row-same-site" : "";
            const note = a.location_note
              ? `<span class="pricing-location-badge">${escapeHtml(a.location_note)}</span>`
              : "";
            return `<tr class="${rowClass}">
          <td>${escapeHtml(a.award_date || "—")}</td>
          <td>${escapeHtml(a.recipient_name || "—")}</td>
          <td>${formatMoney(a.award_amount)}</td>
          <td>${note}${escapeHtml(a.performance_location || "—")}</td>
          <td>${escapeHtml(a.awarding_agency || "—")}</td>
        </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function renderCompetitiveSection(competitive) {
  const c = competitive || {};
  return `
    <div class="pricing-tier pricing-tier-competitive">
      <h4 class="pricing-tier-title">Competitive intelligence</h4>
      <div class="pricing-stats">
        <div class="pricing-stat pricing-stat-wide">
          <span class="pricing-stat-label">Most frequent winner (region)</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(c.most_frequent_winner || "—")}</span>
        </div>
        <div class="pricing-stat pricing-stat-wide">
          <span class="pricing-stat-label">Likely incumbent</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(c.incumbent || "Not identified")}</span>
        </div>
      </div>
      ${c.incumbent_note ? `<p class="pricing-note pricing-incumbent-note">${escapeHtml(c.incumbent_note)}</p>` : ""}
    </div>`;
}

function renderOutcomeSection(data, noticeId) {
  const status = data.status || "reviewing";
  const showOutcome = ["won", "lost", "bidding"].includes(status) || data.awarded_amount;
  if (!showOutcome && status !== "reviewing") return "";
  return `
    <div class="pricing-tier pricing-tier-outcome" data-notice-id="${escapeHtml(noticeId)}">
      <h4 class="pricing-tier-title">Outcome & winning bid</h4>
      <p class="pricing-note">When you learn the result, record the winning bid to improve your internal pricing database.</p>
      <label class="filter-label">Status</label>
      <select class="settings-input outcome-status-select" data-field="status">
        <option value="reviewing" ${status === "reviewing" ? "selected" : ""}>Reviewing</option>
        <option value="bidding" ${status === "bidding" ? "selected" : ""}>Bidding</option>
        <option value="won" ${status === "won" ? "selected" : ""}>Won</option>
        <option value="lost" ${status === "lost" ? "selected" : ""}>Lost</option>
      </select>
      <label class="filter-label">Winning bid amount</label>
      <input type="number" class="settings-input outcome-awarded-input" data-field="awarded_amount" value="${data.awarded_amount ?? data.pws?.awarded_amount ?? ""}" step="0.01" placeholder="Your bid if won, competitor bid if lost">
      <p class="sub-save-hint" data-outcome-hint hidden>Saved</p>
    </div>`;
}

function renderBidCalculator(data) {
  const subDefault = data.selected_sub_quote || "";
  const internal = data.internal || {};
  const optionYears = Number(data.pws?.option_years) || 0;
  const margin = data.effective_margin_pct ?? data.margin_percentage ?? 20;
  const marginCustom = data.margin_percentage != null;
  return `
    <div class="pricing-tier pricing-tier-calculator" id="bid-calculator" data-notice-id="${escapeHtml(data.notice_id || "")}" data-bid-low="${internal.recommended_bid_low ?? ""}" data-bid-high="${internal.recommended_bid_high ?? ""}" data-option-years="${optionYears}">
      <h4 class="pricing-tier-title">Bid calculator</h4>
      <label class="filter-label">Sub quote (annual)</label>
      <input type="number" id="calc-sub-quote" class="settings-input" value="${subDefault}" step="0.01" placeholder="From selected sub">
      <label class="filter-label">Your margin <span id="calc-margin-label">${margin}%</span></label>
      <input type="range" id="calc-margin" min="10" max="35" value="${margin}" step="1">
      <p class="detail-note">${marginCustom ? "Custom margin saved for this contract." : "Using your default margin from Settings — adjust here to override for this contract."}</p>
      <p class="sub-save-hint" id="calc-margin-hint" hidden>Saved</p>
      <div class="pricing-stats">
        <div class="pricing-stat pricing-stat-highlight">
          <span class="pricing-stat-label">Your bid</span>
          <span class="pricing-stat-value" id="calc-your-bid">—</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Annual profit</span>
          <span class="pricing-stat-value" id="calc-annual-profit">—</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">5-year profit</span>
          <span class="pricing-stat-value" id="calc-five-year">—</span>
        </div>
      </div>
      <p class="pricing-note" id="calc-range-status"></p>
      <div id="calc-option-years"></div>
    </div>`;
}

function updateBidCalculator() {
  const calc = document.getElementById("bid-calculator");
  if (!calc) return;
  const sub = Number(document.getElementById("calc-sub-quote")?.value);
  const marginPct = Number(document.getElementById("calc-margin")?.value || 20);
  const marginLabel = document.getElementById("calc-margin-label");
  if (marginLabel) marginLabel.textContent = `${marginPct}%`;

  const bidEl = document.getElementById("calc-your-bid");
  const profitEl = document.getElementById("calc-annual-profit");
  const fiveEl = document.getElementById("calc-five-year");
  const statusEl = document.getElementById("calc-range-status");
  const optionEl = document.getElementById("calc-option-years");

  if (!sub || sub <= 0) {
    if (bidEl) bidEl.textContent = "—";
    if (profitEl) profitEl.textContent = "—";
    if (fiveEl) fiveEl.textContent = "—";
    return;
  }

  const margin = marginPct / 100;
  const bid = sub / (1 - margin);
  const profit = bid - sub;
  if (bidEl) bidEl.textContent = formatMoney(bid);
  if (profitEl) profitEl.textContent = formatMoney(profit);
  if (fiveEl) fiveEl.textContent = formatMoney(profit * 5);

  const low = Number(calc.dataset.bidLow);
  const high = Number(calc.dataset.bidHigh);
  if (statusEl && low && high) {
    statusEl.className = "pricing-note";
    if (bid >= low && bid <= high) {
      statusEl.textContent = "Within your internal recommended bid range.";
      statusEl.classList.add("calc-range-green");
    } else if (bid < low * 0.9 || bid > high * 1.15) {
      statusEl.textContent =
        bid > high * 1.15
          ? "Your bid is significantly above the regional average. Consider adjusting your margin."
          : "Your bid is below the aggressive end of your internal range.";
      statusEl.classList.add("calc-range-red");
    } else {
      statusEl.textContent = "Slightly outside internal recommended range.";
      statusEl.classList.add("calc-range-yellow");
    }
  }

  const optYears = Number(calc.dataset.optionYears) || 0;
  if (optionEl && optYears > 0) {
    const lines = [];
    for (let y = 0; y <= optYears; y++) {
      const amt = bid * Math.pow(1.03, y);
      lines.push(`Option year ${y}: ${formatMoney(amt)}`);
    }
    optionEl.innerHTML = `<p class="pricing-note"><strong>Option years (3% annual increase):</strong><br>${lines.join("<br>")}</p>`;
  }
}

function bindBidCalculator() {
  document.getElementById("calc-sub-quote")?.addEventListener("input", updateBidCalculator);
  document.getElementById("calc-margin")?.addEventListener("input", () => {
    updateBidCalculator();
    scheduleMarginSave();
  });
  updateBidCalculator();
}

let marginSaveTimer = null;
function scheduleMarginSave() {
  const calc = document.getElementById("bid-calculator");
  const noticeId = calc?.dataset.noticeId;
  if (!noticeId) return;
  const margin = Number(document.getElementById("calc-margin")?.value);
  if (!Number.isFinite(margin)) return;
  clearTimeout(marginSaveTimer);
  marginSaveTimer = setTimeout(() => {
    saveContractOutcome(noticeId, { margin_percentage: margin })
      .then((data) => {
        if (typeof mergeContractUpdate === "function") mergeContractUpdate(data);
        if (typeof renderCards === "function") renderCards();
        const hint = document.getElementById("calc-margin-hint");
        if (hint) {
          hint.hidden = false;
          setTimeout(() => { hint.hidden = true; }, 1500);
        }
        const note = calc?.querySelector(".detail-note");
        if (note) note.textContent = "Custom margin saved for this contract.";
      })
      .catch((e) => showSyncStatus(e.message, true));
  }, 450);
}

async function saveContractOutcome(noticeId, payload) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not save outcome");
  return data;
}

function bindOutcomeSection(noticeId) {
  const section = document.querySelector(`.pricing-tier-outcome[data-notice-id="${noticeId}"]`);
  if (!section) return;
  const save = () => {
    const payload = {
      status: section.querySelector('[data-field="status"]')?.value,
      awarded_amount: section.querySelector('[data-field="awarded_amount"]')?.value
        ? Number(section.querySelector('[data-field="awarded_amount"]').value)
        : null,
    };
    saveContractOutcome(noticeId, payload)
      .then(() => {
        const hint = section.querySelector("[data-outcome-hint]");
        if (hint) {
          hint.hidden = false;
          setTimeout(() => { hint.hidden = true; }, 1500);
        }
      })
      .catch((e) => showSyncStatus(e.message, true));
  };
  section.querySelectorAll("[data-field]").forEach((el) => {
    el.addEventListener("change", save);
    if (el.type === "number") el.addEventListener("blur", save);
  });
}

function renderFullPricingPanel(data) {
  const noticeId = data.notice_id || "";
  return `
    <div class="pricing-panel" id="pricing-panel">
      ${renderPwsSection(data.pws)}
      ${renderSiteHistorySection(data.site_history)}
      ${renderInternalPricingSection(data.internal)}
      ${renderRegionalBenchmarkSection(data.regional_benchmark)}
      ${renderCompetitiveSection(data.competitive)}
      ${renderOutcomeSection(data, noticeId)}
      ${renderBidCalculator(data)}
    </div>`;
}

async function loadPricingIntel(noticeId, refresh = false, containerId = "pricing-panel") {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.className = "pricing-panel pricing-panel-loading";
  container.innerHTML = `<p class="pricing-loading">Loading pricing intelligence…</p>`;

  try {
    const url = `/api/contracts/${encodeURIComponent(noticeId)}/pricing${refresh ? "?refresh=true" : ""}`;
    const res = await apiFetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Pricing lookup failed");
    const html = renderFullPricingPanel(data);
    container.outerHTML = html;
    bindBidCalculator();
    bindOutcomeSection(noticeId);
  } catch (err) {
    container.className = "pricing-panel pricing-panel-error";
    container.innerHTML = `<p>${escapeHtml(err.message || "Could not load pricing data.")}</p>`;
  }
}

async function loadPricingDashboard() {
  const container = document.getElementById("pricing-dashboard-content");
  if (!container) return;
  container.innerHTML = `<p class="pricing-loading">Loading pricing database…</p>`;
  try {
    const res = await apiFetch("/api/pricing/dashboard");
    const data = await res.json();
    container.innerHTML = renderPricingDashboard(data);
  } catch (err) {
    container.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderPricingDashboard(data) {
  const naicsRows = Object.entries(data.by_naics || {})
    .map(([code, n]) => `<tr><td>${escapeHtml(code)}</td><td>${n}</td></tr>`)
    .join("");
  const stateRows = Object.entries(data.by_state || {})
    .map(([st, n]) => `<tr><td>${escapeHtml(st)}</td><td>${n}</td></tr>`)
    .join("");
  const heatRows = Object.entries(data.avg_price_per_sqft_per_visit_by_state || {})
    .map(([st, rate]) => {
      const level = rate > 0.15 ? "heat-high" : rate > 0.08 ? "heat-mid" : "heat-low";
      return `<tr><td>${escapeHtml(st)}</td><td class="heat-cell ${level}">${formatUnitRate(rate)}</td></tr>`;
    })
    .join("");
  const wr = data.win_rate_by_price_range || {};
  const marginRows = Object.entries(data.recommended_margin_by_region_pct || {})
    .map(([st, pct]) => `<tr><td>${escapeHtml(st)}</td><td>${pct}%</td></tr>`)
    .join("");

  return `
    <h2>Pricing intelligence</h2>
    <p class="settings-help">Your internal database grows as Claude extracts PWS data and you record winning bids.</p>
    <div class="pricing-stats">
      <div class="pricing-stat"><span class="pricing-stat-label">Contracts with scope data</span><span class="pricing-stat-value">${data.total_in_database ?? 0}</span></div>
      <div class="pricing-stat"><span class="pricing-stat-label">With unit rates</span><span class="pricing-stat-value">${data.total_with_unit_rates ?? 0}</span></div>
      <div class="pricing-stat"><span class="pricing-stat-label">Overall win rate</span><span class="pricing-stat-value">${data.win_rate_overall != null ? `${Math.round(data.win_rate_overall * 100)}%` : "—"}</span></div>
    </div>
    <h3>By NAICS</h3>
    <table class="pricing-table"><thead><tr><th>NAICS</th><th>Contracts</th></tr></thead><tbody>${naicsRows || '<tr><td colspan="2">No data yet</td></tr>'}</tbody></table>
    <h3>By state</h3>
    <table class="pricing-table"><thead><tr><th>State</th><th>Contracts</th></tr></thead><tbody>${stateRows || '<tr><td colspan="2">No data yet</td></tr>'}</tbody></table>
    <h3>Avg $/sq ft/visit by state</h3>
    <table class="pricing-table pricing-heatmap"><thead><tr><th>State</th><th>Avg rate</th></tr></thead><tbody>${heatRows || '<tr><td colspan="2">Record winning bids to populate</td></tr>'}</tbody></table>
    <h3>Win rate by bid position</h3>
    <table class="pricing-table"><thead><tr><th>Range</th><th>Won</th><th>Lost</th></tr></thead><tbody>
      <tr><td>Low (aggressive)</td><td>${wr.low?.won ?? 0}</td><td>${wr.low?.lost ?? 0}</td></tr>
      <tr><td>Mid (recommended)</td><td>${wr.mid?.won ?? 0}</td><td>${wr.mid?.lost ?? 0}</td></tr>
      <tr><td>High (conservative)</td><td>${wr.high?.won ?? 0}</td><td>${wr.high?.lost ?? 0}</td></tr>
    </tbody></table>
    <h3>Recommended margin by region (won contracts)</h3>
    <table class="pricing-table"><thead><tr><th>State</th><th>Avg margin</th></tr></thead><tbody>${marginRows || '<tr><td colspan="2">Win contracts to build margin history</td></tr>'}</tbody></table>`;
}

function bindPricingNav() {
  document.getElementById("tab-pricing")?.addEventListener("click", () => {
    if (typeof stopContractSubsPolling === "function") stopContractSubsPolling();
    showView("pricing");
    loadPricingDashboard();
  });
}

document.addEventListener("DOMContentLoaded", bindPricingNav);
