let config = { naics_codes: [], naics_labels: {}, default_min_days: 30, default_min_score: 1 };
let contracts = [];
let activeDetailId = null;
let detailPollTimer = null;

async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = "/login.html";
    throw new Error("Login required");
  }
  return res;
}

function showView(name) {
  document.getElementById("view-dashboard").hidden = name !== "dashboard";
  document.getElementById("view-settings").hidden = name !== "settings";
  document.getElementById("tab-dashboard").classList.toggle("active", name === "dashboard");
  document.getElementById("tab-settings").classList.toggle("active", name === "settings");
  if (name === "settings") loadSettingsPage();
}

async function loadConfig() {
  const res = await apiFetch("/api/config");
  config = await res.json();
  document.getElementById("min-days").value = config.default_min_days;
  document.getElementById("min-days-value").textContent = config.default_min_days;
  document.getElementById("min-score").value = config.default_min_score || 1;
  document.getElementById("min-score-value").textContent = config.default_min_score || 1;

  const sync = config.naics_sync || {};
  document.getElementById("naics-sync-status").textContent =
    `NAICS coverage: ${sync.synced_count || 0}/${sync.total_count || config.naics_codes.length} codes synced from SAM.gov`;

  const labels = config.naics_labels || {};
  const container = document.getElementById("naics-filters");
  container.innerHTML = config.naics_codes
    .map(
      (code) => `
    <label>
      <input type="checkbox" class="naics-check" value="${code}" checked>
      <span class="naics-filter-label">${escapeHtml(code)}</span>
      <span class="naics-filter-desc">${escapeHtml(labels[code] || "Other Services")}</span>
    </label>`
    )
    .join("");
}

function selectedNaics() {
  return [...document.querySelectorAll(".naics-check:checked")].map((el) => el.value);
}

function buildQuery() {
  const params = new URLSearchParams();
  const naics = selectedNaics();
  if (naics.length) params.set("naics", naics.join(","));
  params.set("min_days", document.getElementById("min-days").value);
  params.set("min_score", document.getElementById("min-score").value);
  const agency = document.getElementById("agency-filter").value.trim();
  if (agency) params.set("agency", agency);
  if (document.getElementById("pursue-only").checked) params.set("pursue_only", "true");
  return params.toString();
}

function cardTone(c) {
  if (c.pursue === true) return "pursue";
  if (c.pursue === false) return "skip";
  if (c.score != null && c.score >= 5 && c.score <= 7) return "maybe";
  return "unscreened";
}

function screeningBadge(c) {
  const tone = cardTone(c);
  if (tone === "pursue") return `<span class="badge badge-pursue">Pursue${c.score != null ? ` · ${c.score}/10` : ""}</span>`;
  if (tone === "skip") return `<span class="badge badge-skip">Skip${c.score != null ? ` · ${c.score}/10` : ""}</span>`;
  if (tone === "maybe") return `<span class="badge badge-maybe">Maybe · ${c.score}/10</span>`;
  return `<span class="badge badge-pending">Not screened</span>`;
}

function formatDue(c) {
  if (!c.due_date) return { main: "Due date unknown", sub: "", urgent: false };
  const days = c.days_until_due;
  return {
    main: new Date(c.due_date + "T00:00:00").toLocaleDateString("en-US", {
      month: "short", day: "numeric", year: "numeric",
    }),
    sub: days != null ? `${days} days left` : "",
    urgent: days != null && days <= 21,
  };
}

function renderCards() {
  const container = document.getElementById("cards");
  document.getElementById("results-count").textContent =
    `${contracts.length} contract${contracts.length === 1 ? "" : "s"}`;

  if (!contracts.length) {
    container.innerHTML =
      '<div class="empty">No contracts match your filters. Try adjusting filters or sync from SAM.gov.</div>';
    return;
  }

  container.innerHTML = contracts.map((c) => {
    const tone = cardTone(c);
    const due = formatDue(c);
    const subType = c.sub_type_needed || "Not screened yet";
    const summary = c.plain_english_summary || c.executive_summary;
    const headline = summary ? firstSentence(summary, 160) : null;
    const pricing = c.pricing_intelligence || c.analysis?.pricing_intelligence;
    const bidPreview = pricing?.recommended_bid_low && pricing?.recommended_bid_high
      ? `<div class="card-section card-section-bid">
           <span class="card-label">Recommended bid</span>
           <span class="card-bid-range">${formatMoney(pricing.recommended_bid_low)} – ${formatMoney(pricing.recommended_bid_high)}</span>
         </div>`
      : "";
    const titleBlock = headline
      ? `<div class="card-section card-section-summary">
           <span class="card-label">What it is</span>
           <h3 class="card-title">${escapeHtml(headline)}</h3>
           <p class="card-official-title">${escapeHtml(c.title)}</p>
         </div>`
      : `<div class="card-section card-section-summary">
           <span class="card-label">What it is</span>
           <h3 class="card-title">${escapeHtml(c.title)}</h3>
           <p class="card-pending-note">Plain-English summary being generated…</p>
         </div>`;
    const naicsLine = c.naics_display || c.naics_code || "";
    return `
    <article class="card card-${tone}" data-id="${c.notice_id}">
      <div class="card-top">
        ${screeningBadge(c)}
        <div class="card-due${due.urgent ? " card-due-urgent" : ""}">
          <span class="card-due-label">Due</span>
          <span class="card-due-date">${escapeHtml(due.main)}</span>
          ${due.sub ? `<span class="card-due-days">${escapeHtml(due.sub)}</span>` : ""}
        </div>
      </div>
      ${titleBlock}
      ${bidPreview}
      <div class="card-section card-section-location">
        <span class="card-label">Where</span>
        <p class="card-meta">${escapeHtml(c.location || "Location unknown")}</p>
        <p class="card-meta card-agency">${escapeHtml(c.agency || "Unknown agency")}</p>
      </div>
      <div class="card-section card-section-footer">
        <p class="card-subtype"><span class="card-label-inline">Sub type:</span> ${escapeHtml(subType)}</p>
        <p class="card-meta card-naics"><span class="card-label-inline">NAICS:</span> ${escapeHtml(naicsLine)}</p>
      </div>
    </article>`;
  }).join("");

  container.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", () => openDetail(el.dataset.id));
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

async function loadContracts() {
  const res = await apiFetch(`/api/contracts?${buildQuery()}`);
  const data = await res.json();
  contracts = data.contracts || [];
  renderCards();
}

function wrapDetailSection(title, innerHtml, extraClass = "") {
  return `
    <section class="detail-section ${extraClass}">
      <h3 class="detail-section-heading">${escapeHtml(title)}</h3>
      ${innerHtml}
    </section>`;
}

function getContractSummary(c) {
  return c.plain_english_summary || c.executive_summary || c.analysis?.plain_english_summary || c.analysis?.executive_summary;
}

function stopDetailPolling() {
  if (detailPollTimer) {
    clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
}

function buildSummaryInner(c, analyzing = false) {
  const summary = getContractSummary(c);
  if (summary) {
    return `<div class="executive-summary">${formatSummaryHtml(summary)}</div>`;
  }
  if (analyzing) {
    return `<div class="executive-summary-placeholder analyzing">
      <p class="analyzing-title">Analyzing this contract…</p>
      <p class="detail-note">Reading attachments, checking historical pricing, and writing your plain-English summary. This usually takes 30–90 seconds.</p>
    </div>`;
  }
  return `<div class="executive-summary-placeholder">
    <p>Summary not available yet.</p>
  </div>`;
}

function renderDetailModal(c, { analyzing = false } = {}) {
  const due = formatDue(c);
  const summary = getContractSummary(c);
  const pricingIntel = c.pricing_intelligence || c.analysis?.pricing_intelligence;
  const summaryInner = buildSummaryInner(c, analyzing && !summary);
  const pricingInner = pricingIntel
    ? renderClaudePricingPanel(pricingIntel, c.pricing_intel)
    : `<div id="pricing-panel" class="pricing-panel pricing-panel-loading">
         <p class="pricing-loading">Loading comparable awards from USAspending.gov…</p>
       </div>`;
  const redFlags = c.red_flags?.length
    ? `<ul class="detail-list">${c.red_flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
    : "<p>None</p>";
  const attachments = c.analysis?.attachments_reviewed;
  const attachmentNote = attachments?.length
    ? `<p class="detail-note">PDFs reviewed: ${attachments.map(escapeHtml).join(", ")}</p>`
    : "";

  const screeningInner = `
    <div class="detail-grid">
      <div class="detail-item">
        <span class="detail-item-label">Verdict</span>
        <div class="modal-badges">${screeningBadge(c)}</div>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Due date</span>
        <p class="detail-item-value">${escapeHtml(due.main)}${due.sub ? ` · ${escapeHtml(due.sub)}` : ""}</p>
      </div>
      <div class="detail-item detail-item-wide">
        <span class="detail-item-label">Quick reason</span>
        <p class="detail-item-value">${escapeHtml(c.reason || c.analysis?.reason || (analyzing ? "Analysis in progress…" : "-"))}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Sub type needed</span>
        <p class="detail-item-value">${escapeHtml(c.sub_type_needed || c.analysis?.sub_type_needed || (analyzing ? "Analysis in progress…" : "-"))}</p>
      </div>
      <div class="detail-item detail-item-wide">
        <span class="detail-item-label">Red flags</span>
        ${redFlags}
      </div>
    </div>
    ${attachmentNote}`;

  const contractInner = `
    <div class="detail-grid">
      <div class="detail-item detail-item-wide">
        <span class="detail-item-label">Official title</span>
        <p class="detail-item-value">${escapeHtml(c.title)}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Agency</span>
        <p class="detail-item-value">${escapeHtml(c.agency || "-")}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Location</span>
        <p class="detail-item-value">${escapeHtml(c.location || "-")}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">NAICS</span>
        <p class="detail-item-value">${escapeHtml(c.naics_display || c.naics_code || "-")}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Set-aside</span>
        <p class="detail-item-value">${escapeHtml(c.set_aside || "-")}</p>
      </div>
      <div class="detail-item">
        <span class="detail-item-label">Status</span>
        <p class="detail-item-value">${escapeHtml(c.status)}</p>
      </div>
    </div>
    ${c.link ? `<a class="detail-link" href="${escapeHtml(c.link)}" target="_blank" rel="noopener">View on SAM.gov</a>` : ""}`;

  document.getElementById("modal-content").innerHTML = `
    ${wrapDetailSection("Plain English summary", summaryInner, "detail-section-summary")}
    ${wrapDetailSection("Pricing intelligence", pricingInner, "detail-section-pricing")}
    ${wrapDetailSection("Screening verdict", screeningInner, "detail-section-screening")}
    ${wrapDetailSection("Contract details", contractInner, "detail-section-contract")}
  `;
}

async function fetchContract(noticeId) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}`);
  if (!res.ok) return null;
  return res.json();
}

async function requestContractScreening(noticeId) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/screen`, { method: "POST" });
  const data = await res.json();
  if (!res.ok && !data.in_progress) {
    throw new Error(data.detail || "Screening failed");
  }
  return data;
}

function startDetailLiveUpdates(noticeId) {
  stopDetailPolling();
  activeDetailId = noticeId;

  detailPollTimer = setInterval(async () => {
    if (activeDetailId !== noticeId || document.getElementById("modal").hidden) {
      stopDetailPolling();
      return;
    }
    try {
      const c = await fetchContract(noticeId);
      if (!c) return;
      if (getContractSummary(c)) {
        renderDetailModal(c);
        await loadContracts();
        stopDetailPolling();
      }
    } catch {
      /* keep polling */
    }
  }, 3000);
}

async function beginAutoAnalysis(noticeId) {
  startDetailLiveUpdates(noticeId);

  try {
    await requestContractScreening(noticeId);
    const c = await fetchContract(noticeId);
    if (c && getContractSummary(c)) {
      renderDetailModal(c);
      if (!c.pricing_intelligence && !c.analysis?.pricing_intelligence) {
        loadPricingIntel(noticeId);
      }
      await loadContracts();
      stopDetailPolling();
    }
  } catch (err) {
    if (err.message !== "Login required") {
      showSyncStatus(err.message, true);
    }
  }
}

async function openDetail(noticeId) {
  stopDetailPolling();
  activeDetailId = noticeId;

  const c = await fetchContract(noticeId);
  if (!c) return;

  const summary = getContractSummary(c);
  const pricingIntel = c.pricing_intelligence || c.analysis?.pricing_intelligence;

  renderDetailModal(c, { analyzing: !summary });
  document.getElementById("modal").hidden = false;

  if (!pricingIntel) {
    loadPricingIntel(noticeId);
  }

  if (!summary) {
    beginAutoAnalysis(noticeId);
  }
}

function formatSummaryHtml(text) {
  return escapeHtml(text)
    .split(/\n\n+/)
    .map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`)
    .join("");
}

function firstSentence(text, maxLen = 180) {
  const trimmed = (text || "").trim();
  if (!trimmed) return "";
  const match = trimmed.match(/^[\s\S]+?[.!?](?:\s|$)/);
  const sentence = match ? match[0].trim() : trimmed;
  if (sentence.length <= maxLen) return sentence;
  return `${sentence.slice(0, maxLen).trim()}…`;
}

function formatMoney(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "string" && value.trim().startsWith("$")) return value.trim();
  const num = Number(String(value).replace(/[^0-9.-]/g, ""));
  if (Number.isNaN(num)) return String(value);
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(num);
}

function formatAwardDate(award) {
  const raw = award?.award_date || award?.start_date;
  if (!raw) return "Date unknown";
  const d = new Date(`${String(raw).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(d.getTime())) return String(raw);
  const label = d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  if (award?.days_ago == null) return label;
  if (award.days_ago <= 365) return `${label} (${award.days_ago} days ago)`;
  const years = (award.days_ago / 365).toFixed(1);
  return `${label} (${years} yrs ago)`;
}

function sortAwardsByDistance(awards) {
  return [...(awards || [])].sort((a, b) => {
    const da = a?.distance_miles;
    const db = b?.distance_miles;
    if (da == null && db == null) {
      const ad = a?.award_date || a?.start_date || "";
      const bd = b?.award_date || b?.start_date || "";
      return bd.localeCompare(ad);
    }
    if (da == null) return 1;
    if (db == null) return -1;
    if (da !== db) return da - db;
    const ad = a?.award_date || a?.start_date || "";
    const bd = b?.award_date || b?.start_date || "";
    return bd.localeCompare(ad);
  });
}

function formatAwardDistance(award) {
  if (award?.distance_label) return award.distance_label;
  return "—";
}

function awardsTableCaption(intel) {
  const origin = intel?.origin_location?.label || formatPricingLocationScope(intel);
  const closest = intel?.closest_award_label;
  let text = `Sorted closest to your contract location (${origin}) first — best nearby subcontractor markets.`;
  if (closest) {
    text += ` Nearest comparable award: ${closest}.`;
  }
  return text;
}

function formatPricingLocationScope(intel) {
  if (!intel) return "this work area";
  if (intel.location_scope) return intel.location_scope;
  return intel.state_code || "this work area";
}

function formatAwardLocation(award) {
  if (award?.performance_location) return award.performance_location;
  const parts = [award?.performance_city, award?.performance_state, award?.performance_zip].filter(Boolean);
  return parts.length ? parts.join(", ") : "—";
}

function renderAwardsTable(awards, intel) {
  const rows = sortAwardsByDistance(awards);
  if (!rows.length) return "";
  return `
    <p class="pricing-table-caption">${escapeHtml(awardsTableCaption(intel))}</p>
    <table class="pricing-table">
    <thead>
      <tr><th>Distance</th><th>Work location</th><th>Award date</th><th>Recipient</th><th>Amount</th><th>Agency</th></tr>
    </thead>
    <tbody>
      ${rows.map((a, index) => `
        <tr class="${[
          index === 0 && a.distance_miles != null ? "pricing-row-closest" : "",
          a.days_ago != null && a.days_ago <= 365 ? "pricing-row-recent" : "",
          a.distance_same_state === false ? "pricing-row-other-state" : "",
        ].filter(Boolean).join(" ")}">
          <td class="pricing-distance">${escapeHtml(formatAwardDistance(a))}</td>
          <td>${escapeHtml(formatAwardLocation(a))}</td>
          <td>${escapeHtml(formatAwardDate(a))}</td>
          <td>${escapeHtml(a.recipient_name || "—")}</td>
          <td>${formatMoney(a.award_amount)}</td>
          <td>${escapeHtml(a.awarding_agency || "—")}</td>
        </tr>`).join("")}
    </tbody>
  </table>`;
}

function renderClaudePricingPanel(pricing, rawIntel) {
  const bidLow = formatMoney(pricing.recommended_bid_low);
  const bidHigh = formatMoney(pricing.recommended_bid_high);
  const competition = pricing.competition_level
    ? `${pricing.competition_level.charAt(0).toUpperCase()}${pricing.competition_level.slice(1)} competition`
    : "—";
  const confidence = pricing.pricing_confidence
    ? `${pricing.pricing_confidence.charAt(0).toUpperCase()}${pricing.pricing_confidence.slice(1)} confidence`
    : "—";
  const incumbent = pricing.incumbent || rawIntel?.likely_incumbent || "Not identified";
  const winner = pricing.most_frequent_winner || "—";
  const recentCount = rawIntel?.awards_last_12_months;
  const locationScope = formatPricingLocationScope(rawIntel);
  const awardsNote = rawIntel?.awards_count
    ? `${rawIntel.awards_count} comparable awards where work was performed in ${locationScope}${recentCount != null ? ` · ${recentCount} in last 12 months` : ""} · NAICS ${rawIntel.naics_code || ""}`
    : "Based on USAspending.gov historical data in the same work area";

  const awardsTable = renderAwardsTable(rawIntel?.awards, rawIntel);

  return `
    <div class="pricing-panel">
      <p class="pricing-intro">${escapeHtml(awardsNote)} <span class="pricing-source">(USAspending.gov + Claude analysis · same work area · recent awards weighted higher)</span></p>
      ${rawIntel?.location_scope_note ? `<p class="pricing-note">${escapeHtml(rawIntel.location_scope_note)}</p>` : ""}
      <div class="pricing-bid-hero">
        <span class="pricing-bid-label">Recommended bid range</span>
        <span class="pricing-bid-range">${bidLow} – ${bidHigh}</span>
      </div>
      ${pricing.pricing_summary ? `<p class="pricing-summary">${escapeHtml(pricing.pricing_summary)}</p>` : ""}
      <div class="pricing-stats">
        <div class="pricing-stat">
          <span class="pricing-stat-label">Average historical award</span>
          <span class="pricing-stat-value">${formatMoney(pricing.average_historical_award)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Highest award</span>
          <span class="pricing-stat-value">${formatMoney(pricing.highest_historical_award)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Lowest award</span>
          <span class="pricing-stat-value">${formatMoney(pricing.lowest_historical_award)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Most frequent winner</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(winner)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Incumbent</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(incumbent)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Competition</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(competition)} · ${escapeHtml(confidence)}</span>
        </div>
      </div>
      ${awardsTable}
    </div>`;
}

function renderPricingPanel(intel) {
  if (intel.error) {
    return `
      <div class="pricing-panel pricing-panel-error">
        <p>${escapeHtml(intel.error)}</p>
        ${intel.naics_code ? `<p class="pricing-meta">NAICS ${escapeHtml(intel.naics_code)}${intel.state_code ? ` · ${escapeHtml(intel.state_code)}` : ""}</p>` : ""}
      </div>`;
  }

  const winner = intel.most_frequent_winner
    ? `${intel.most_frequent_winner}${intel.most_frequent_winner_count > 1 ? ` (score ${intel.most_frequent_winner_count})` : ""}`
    : "—";

  const locationScope = formatPricingLocationScope(intel);

  const awardsTable = renderAwardsTable(intel.awards, intel)
    || `<p class="pricing-meta">No comparable awards found for this NAICS in ${escapeHtml(locationScope)} over the last 3 years.</p>`;

  return `
    <div class="pricing-panel">
      <p class="pricing-intro">
        ${intel.awards_with_dates || intel.awards_count} dated contract${(intel.awards_with_dates || intel.awards_count) === 1 ? "" : "s"} where work was performed in
        <strong>${escapeHtml(locationScope)}</strong> · NAICS
        <strong>${escapeHtml(intel.naics_code || "")}</strong>
        ${intel.awards_last_12_months != null ? ` · ${intel.awards_last_12_months} in last 12 months` : ""}
        <span class="pricing-source">(USAspending.gov · same work area · recent awards weighted higher)</span>
      </p>
      ${intel.location_scope_note ? `<p class="pricing-note">${escapeHtml(intel.location_scope_note)}</p>` : ""}
      <div class="pricing-stats">
        <div class="pricing-stat">
          <span class="pricing-stat-label">Weighted average</span>
          <span class="pricing-stat-value">${formatMoney(intel.weighted_average_amount || intel.average_amount)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Highest award</span>
          <span class="pricing-stat-value">${formatMoney(intel.highest_amount)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Lowest award</span>
          <span class="pricing-stat-value">${formatMoney(intel.lowest_amount)}</span>
        </div>
        <div class="pricing-stat pricing-stat-wide">
          <span class="pricing-stat-label">Most frequent winner</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(winner)}</span>
        </div>
        <div class="pricing-stat pricing-stat-highlight">
          <span class="pricing-stat-label">Recommended bid range</span>
          <span class="pricing-stat-value">${formatMoney(intel.recommended_bid_low)} – ${formatMoney(intel.recommended_bid_high)}</span>
        </div>
      </div>
      ${intel.recommended_bid_note ? `<p class="pricing-note">${escapeHtml(intel.recommended_bid_note)}</p>` : ""}
      ${awardsTable}
    </div>`;
}

async function loadPricingIntel(noticeId, refresh = false) {
  const container = document.getElementById("pricing-panel");
  if (!container) return;
  container.className = "pricing-panel pricing-panel-loading";
  container.innerHTML = `<p class="pricing-loading">Loading comparable awards from USAspending.gov…</p>`;

  try {
    const url = `/api/contracts/${encodeURIComponent(noticeId)}/pricing${refresh ? "?refresh=true" : ""}`;
    const res = await apiFetch(url);
    const intel = await res.json();
    if (!res.ok) throw new Error(intel.detail || "Pricing lookup failed");
    container.outerHTML = renderPricingPanel(intel);
  } catch (err) {
    container.className = "pricing-panel pricing-panel-error";
    container.innerHTML = `<p>${escapeHtml(err.message || "Could not load pricing data.")}</p>`;
  }
}

function closeModal() {
  stopDetailPolling();
  activeDetailId = null;
  document.getElementById("modal").hidden = true;
}

function showSyncStatus(message, isError = false) {
  const el = document.getElementById("sync-status");
  el.textContent = message;
  el.hidden = false;
  el.classList.toggle("error", isError);
}

async function runSync(allNaics = false) {
  const btn = document.getElementById(allNaics ? "sync-all-btn" : "refresh-btn");
  const other = document.getElementById(allNaics ? "refresh-btn" : "sync-all-btn");
  btn.disabled = true;
  other.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Syncing...";
  showSyncStatus(allNaics
    ? `Pulling all ${config.naics_codes?.length || 6} NAICS codes from SAM.gov...`
    : "Pulling next NAICS code from SAM.gov (1 API call)...");

  try {
    const url = allNaics ? "/api/sync?all_naics=true" : "/api/sync";
    const res = await apiFetch(url, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Sync failed");
    showSyncStatus(`${data.fetch_status} Saved ${data.new} new, ${data.updated} updated. ${data.total_in_db} total in database.`);
    await loadConfig();
    await loadContracts();
  } catch (err) {
    if (err.message !== "Login required") showSyncStatus(err.message, true);
  } finally {
    btn.disabled = false;
    other.disabled = false;
    btn.textContent = label;
  }
}

async function runScreen() {
  const btn = document.getElementById("screen-btn");
  btn.disabled = true;
  btn.textContent = "Screening...";
  showSyncStatus("Analyzing matching contracts (reads PDF attachments — may take several minutes)...");
  try {
    const res = await apiFetch("/api/screen?limit=25&matching_only=true", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Screening failed");
    let msg = `Screened ${data.screened} contract(s). ${data.pending_remaining} still unscreened in database.`;
    if (data.errors?.length) msg += ` Errors: ${data.errors.length}`;
    showSyncStatus(msg, Boolean(data.errors?.length));
    await loadContracts();
  } catch (err) {
    if (err.message !== "Login required") showSyncStatus(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Screen all matching";
  }
}

async function loadSettingsPage() {
  const res = await apiFetch("/api/settings");
  const data = await res.json();
  document.getElementById("settings-naics").value = (data.naics_codes || []).join(", ");
  document.getElementById("settings-min-days").value = data.min_days_until_due;
  document.getElementById("settings-min-days-value").textContent = data.min_days_until_due;
  document.getElementById("settings-min-score").value = data.min_score_threshold;
  document.getElementById("settings-min-score-value").textContent = data.min_score_threshold;
  document.getElementById("settings-prompt").value = data.screening_prompt || "";
  document.getElementById("prompt-status").textContent = data.screening_prompt_custom
    ? "Using your custom prompt"
    : "Using the built-in default prompt";

  const sched = data.scheduler || {};
  document.getElementById("settings-scheduler-enabled").checked = sched.enabled !== false;
  const hour = String(sched.hour ?? 6).padStart(2, "0");
  const minute = String(sched.minute ?? 0).padStart(2, "0");
  document.getElementById("settings-scheduler-time").value = `${hour}:${minute}`;
  document.getElementById("settings-scheduler-timezone").value = sched.timezone || "America/Denver";

  const keys = data.api_keys || {};
  document.getElementById("api-key-status").innerHTML = `
    <li>SAM.gov: ${keys.sam_gov ? "configured" : "missing"}</li>
    <li>Anthropic: ${keys.anthropic ? "configured" : "missing"}</li>
    <li>PostgreSQL: ${keys.database ? "configured" : "missing"}</li>
  `;

  const schedRes = await apiFetch("/api/scheduler");
  const schedStatus = await schedRes.json();
  document.getElementById("scheduler-status").textContent = schedStatus.enabled
    ? `Next automatic sync: ${formatSchedulerStatus(schedStatus)}`
    : "Daily sync is turned off";
}

function formatSchedulerStatus(sched) {
  const hour = String(sched.hour ?? 6).padStart(2, "0");
  const minute = String(sched.minute ?? 0).padStart(2, "0");
  const tz = (sched.timezone || "America/Denver").replace("America/", "");
  if (sched.next_run) {
    const next = new Date(sched.next_run);
    return `${hour}:${minute} ${tz} · next run ${next.toLocaleString()}`;
  }
  return `${hour}:${minute} ${tz}`;
}

function parseSchedulerTime(value) {
  const [hour, minute] = (value || "06:00").split(":");
  return { hour: Number(hour) || 6, minute: Number(minute) || 0 };
}

async function saveSettings() {
  const btn = document.getElementById("save-settings-btn");
  btn.disabled = true;
  try {
    const naics = document.getElementById("settings-naics").value
      .split(",").map((s) => s.trim()).filter(Boolean);
    const schedTime = parseSchedulerTime(document.getElementById("settings-scheduler-time").value);
    const body = {
      naics_codes: naics,
      min_days_until_due: Number(document.getElementById("settings-min-days").value),
      min_score_threshold: Number(document.getElementById("settings-min-score").value),
      screening_prompt: document.getElementById("settings-prompt").value.trim(),
      scheduler_enabled: document.getElementById("settings-scheduler-enabled").checked,
      scheduler_hour: schedTime.hour,
      scheduler_minute: schedTime.minute,
      scheduler_timezone: document.getElementById("settings-scheduler-timezone").value,
    };
    const res = await apiFetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    showSyncStatus("Settings saved.");
    await loadConfig();
    await loadContracts();
    await loadSettingsPage();
  } catch (err) {
    if (err.message !== "Login required") showSyncStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function resetPrompt() {
  const res = await apiFetch("/api/settings/screening-prompt/reset", { method: "POST" });
  const data = await res.json();
  document.getElementById("settings-prompt").value = data.screening_prompt || "";
  document.getElementById("prompt-status").textContent = "Using the built-in default prompt";
  showSyncStatus("Screening prompt reset to default.");
}

async function logout() {
  await apiFetch("/api/logout", { method: "POST" });
  window.location.href = "/login.html";
}

function bindSlider(inputId, labelId) {
  document.getElementById(inputId).addEventListener("input", (e) => {
    document.getElementById(labelId).textContent = e.target.value;
  });
}

document.getElementById("tab-dashboard").addEventListener("click", () => showView("dashboard"));
document.getElementById("tab-settings").addEventListener("click", () => showView("settings"));
document.getElementById("logout-btn").addEventListener("click", logout);
document.getElementById("apply-filters").addEventListener("click", loadContracts);
document.getElementById("refresh-btn").addEventListener("click", () => runSync(false));
document.getElementById("sync-all-btn").addEventListener("click", () => runSync(true));
document.getElementById("screen-btn").addEventListener("click", runScreen);
document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal-backdrop").addEventListener("click", closeModal);
document.getElementById("save-settings-btn").addEventListener("click", saveSettings);
document.getElementById("reset-prompt-btn").addEventListener("click", resetPrompt);

bindSlider("min-days", "min-days-value");
bindSlider("min-score", "min-score-value");
bindSlider("settings-min-days", "settings-min-days-value");
bindSlider("settings-min-score", "settings-min-score-value");

loadConfig().then(loadContracts);
