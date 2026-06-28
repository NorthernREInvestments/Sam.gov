let config = { naics_codes: [], naics_labels: {}, default_min_days: 10, default_min_score: 1 };
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
  document.getElementById("view-subs").hidden = name !== "subs";
  document.getElementById("view-contract-subs").hidden = name !== "contract-subs";
  document.getElementById("tab-dashboard").classList.toggle("active", name === "dashboard");
  document.getElementById("tab-subs").classList.toggle("active", name === "subs");
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
  const nextNaics = sync.next_naics ? ` · next: ${sync.next_naics}` : "";
  document.getElementById("naics-sync-status").textContent =
    `NAICS coverage: ${sync.synced_count || 0}/${sync.total_count || config.naics_codes.length} codes synced from SAM.gov${nextNaics}`;

  populateSyncNaicsSelect();

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

function selectedSyncNaics() {
  const select = document.getElementById("sync-naics-select");
  return select ? select.value : "";
}

function populateSyncNaicsSelect() {
  const select = document.getElementById("sync-naics-select");
  if (!select) return;
  const labels = config.naics_labels || {};
  const sync = config.naics_sync || {};
  const nextNaics = sync.next_naics || "";
  const options = [
    `<option value="">Next in rotation${nextNaics ? ` (${nextNaics})` : ""}</option>`,
    ...(config.naics_codes || []).map(
      (code) =>
        `<option value="${code}">${escapeHtml(code)} — ${escapeHtml(labels[code] || "Other Services")}</option>`
    ),
  ];
  select.innerHTML = options.join("");
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
    const annualBid = recommendedAnnualBid(c.pricing_intel);
    const bidPreview = annualBid
      ? `<div class="card-section card-section-bid">
           <span class="card-label">Recommended annual bid</span>
           <span class="card-bid-range">${formatMoney(annualBid)}</span>
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
    const attachmentsHtml = renderCardAttachments(c);
    const networkBanner = typeof renderNetworkBanner === "function" ? renderNetworkBanner(c) : "";
    const subSummaryCard = typeof renderCardSubSummary === "function" ? renderCardSubSummary(c) : "";
    const findSubsBtn = typeof renderFindSubsButton === "function" ? renderFindSubsButton(c) : "";
    return `
    <article class="card card-${tone}" data-id="${c.notice_id}">
      <div class="card-top">
        ${screeningBadge(c)}
        ${c.security_clearance_required ? '<span class="badge badge-clearance">Clearance required</span>' : ""}
        <div class="card-due${due.urgent ? " card-due-urgent" : ""}">
          <span class="card-due-label">Due</span>
          <span class="card-due-date">${escapeHtml(due.main)}</span>
          ${due.sub ? `<span class="card-due-days">${escapeHtml(due.sub)}</span>` : ""}
        </div>
      </div>
      ${titleBlock}
      ${networkBanner}
      ${subSummaryCard}
      ${bidPreview}
      ${attachmentsHtml}
      <div class="card-section card-section-location">
        <span class="card-label">Where</span>
        <p class="card-meta">${escapeHtml(c.location || "Location unknown")}</p>
        <p class="card-meta card-agency">${escapeHtml(c.agency || "Unknown agency")}</p>
      </div>
      <div class="card-section card-section-footer">
        <p class="card-subtype"><span class="card-label-inline">Sub type:</span> ${escapeHtml(subType)}</p>
        <p class="card-meta card-naics"><span class="card-label-inline">NAICS:</span> ${escapeHtml(naicsLine)}</p>
        ${findSubsBtn}
      </div>
    </article>`;
  }).join("");

  container.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select, textarea")) return;
      openDetail(el.dataset.id);
    });
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

let cardPollTimer = null;

function cardNeedsPolling(c) {
  const subsSearching =
    c.sub_search_status === "searching" || (c.sub_summary || {}).status === "searching";
  const attachmentsPending = !(c.sam_attachments || []).length;
  return subsSearching || attachmentsPending;
}

function manageCardPolling() {
  const shouldPoll = contracts.some(cardNeedsPolling);
  if (shouldPoll && !cardPollTimer) {
    cardPollTimer = setInterval(async () => {
      if (!contracts.some(cardNeedsPolling)) {
        clearInterval(cardPollTimer);
        cardPollTimer = null;
        return;
      }
      await loadContractsQuiet();
    }, 4000);
  } else if (!shouldPoll && cardPollTimer) {
    clearInterval(cardPollTimer);
    cardPollTimer = null;
  }
}

async function loadContracts() {
  const res = await apiFetch(`/api/contracts?${buildQuery()}`);
  const data = await res.json();
  contracts = data.contracts || [];
  renderCards();
  manageCardPolling();
}

async function loadContractsQuiet() {
  const res = await apiFetch(`/api/contracts?${buildQuery()}`);
  const data = await res.json();
  contracts = data.contracts || [];
  renderCards();
  manageCardPolling();
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

function renderDocumentAccessBanner(c) {
  const access = c.sam_raw?.documentAccess || c.document_access || c.analysis?.document_access;
  const attachments = c.sam_raw?.opportunityAttachments || c.sam_attachments || c.analysis?.sam_attachments || [];
  const links = c.external_links || c.analysis?.external_links || c.sam_raw?.opportunityLinks || [];
  if (!access && !attachments.length) return "";

  const samLink = access?.sam_gov_link || c.link;
  const attachmentItems = attachments.length
    ? `<ul class="document-link-list">${attachments.map((item) => {
        const label = item.description || item.url || "Attachment";
        if (item.type === "link" && item.url) {
          return `<li><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a></li>`;
        }
        return `<li>${escapeHtml(label)}${item.type === "file" ? " (SAM.gov file)" : ""}</li>`;
      }).join("")}</ul>`
    : links.length
      ? `<ul class="document-link-list">${links.map((item) => {
          const url = item.url || item;
          const label = item.label || url;
          return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a></li>`;
        }).join("")}</ul>`
      : "";

  const samCta = samLink
    ? `<p class="document-access-cta"><a class="detail-link" href="${escapeHtml(samLink)}" target="_blank" rel="noopener">View full posting on SAM.gov</a></p>`
    : "";

  return `
    <div class="document-access-banner ${access?.requires_external_portal ? "document-access-external" : ""}">
      <p class="document-access-title">${escapeHtml(access?.summary || "SAM.gov attachments")}</p>
      ${attachmentItems}
      ${samCta}
    </div>`;
}

function renderCardAttachments(c) {
  const attachments = c.sam_attachments || c.analysis?.sam_attachments || [];
  const access = c.document_access || c.analysis?.document_access;
  if (!attachments.length) {
    if (access?.summary) {
      return `<div class="card-section card-section-attachments">
        <span class="card-label">Attachments</span>
        <p class="card-meta card-attachments-pending">${escapeHtml(access.summary)}</p>
      </div>`;
    }
    return `<div class="card-section card-section-attachments">
      <span class="card-label">Attachments</span>
      <p class="card-meta card-attachments-pending">Loading from SAM.gov…</p>
    </div>`;
  }
  const items = attachments.map((item) => {
    const label = item.description || item.url || "Attachment";
    const url = item.url || item.download_url;
    const tag = item.source === "piee" ? " (PIEE)" : item.type === "file" ? " (file)" : "";
    if (url) {
      return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escapeHtml(label)}${tag}</a></li>`;
    }
    return `<li>${escapeHtml(label)}${tag}</li>`;
  }).join("");
  const summary = access?.summary
    ? `<p class="card-meta card-attachments-summary">${escapeHtml(access.summary)}</p>`
    : "";
  return `<div class="card-section card-section-attachments">
    <span class="card-label">Attachments (${attachments.length})</span>
    ${summary}
    <ul class="card-attachment-list">${items}</ul>
  </div>`;
}

function buildSummaryInner(c, analyzing = false) {
  const summary = getContractSummary(c);
  const documentBanner = renderDocumentAccessBanner(c);
  if (summary) {
    return `${documentBanner}<div class="executive-summary">${formatSummaryHtml(summary)}</div>`;
  }
  if (analyzing) {
    return `${documentBanner}<div class="executive-summary-placeholder analyzing">
      <p class="analyzing-title">Analyzing this contract…</p>
      <p class="detail-note">Reading the posting description, checking linked documents, historical pricing, and writing your plain-English summary. This usually takes 30–90 seconds.</p>
    </div>`;
  }
  return `${documentBanner}<div class="executive-summary-placeholder">
    <p>Summary not available yet.</p>
  </div>`;
}

function renderDetailModal(c, { analyzing = false } = {}) {
  const summaryInner = buildSummaryInner(c, analyzing && !getContractSummary(c));
  const pricingIntel = c.pricing_intelligence || c.analysis?.pricing_intelligence;
  const pricingInner = pricingIntel
    ? renderClaudePricingPanel(pricingIntel, c.pricing_intel)
    : `<div id="pricing-panel" class="pricing-panel pricing-panel-loading">
         <p class="pricing-loading">Loading comparable awards from USAspending.gov…</p>
       </div>`;
  const subsLink = typeof renderSubSummaryLink === "function" ? renderSubSummaryLink(c) : "";
  const pursueSection = typeof renderPursueSection === "function" ? renderPursueSection(c) : "";

  document.getElementById("modal-content").innerHTML = `
    <div class="detail-header">
      <h2 class="detail-title">${escapeHtml(c.title)}</h2>
      <p class="detail-agency">${escapeHtml(c.agency || "Unknown agency")}</p>
    </div>
    ${wrapDetailSection("Plain English summary", summaryInner, "detail-section-summary")}
    ${wrapDetailSection("Historical bids", pricingInner, "detail-section-pricing")}
    ${wrapDetailSection("Recommended subs", subsLink || "<p>Run Find Subs to search Google Places.</p>", "detail-section-subs")}
    ${wrapDetailSection("Pursue", pursueSection, "detail-section-pursue")}
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

function formatClearanceRequired(c) {
  const flagged = c.security_clearance_required ?? c.analysis?.security_clearance_required;
  if (flagged === true) return "Required (or restricted access)";
  if (flagged === false) return "Not required";
  return "Unknown — check solicitation";
}

function recommendedAnnualBid(intel) {
  if (!intel) return null;
  return intel.recommended_annual_bid
    ?? intel.unit_rate_summary?.recommended_annual_bid
    ?? null;
}

function recommendedBidFormula(intel) {
  if (!intel) return "";
  return intel.recommended_bid_formula
    ?? intel.unit_rate_summary?.recommended_bid_formula
    ?? "";
}

function formatUnitRate(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (n >= 1) return `$${n.toFixed(2)}/sq ft/visit`;
  if (n >= 0.01) return `$${n.toFixed(3)}/sq ft/visit`;
  return `$${n.toFixed(4)}/sq ft/visit`;
}

function formatSqFt(value) {
  if (value == null) return "—";
  return `${Number(value).toLocaleString()} sq ft`;
}

function awardsTableCaption(intel) {
  const origin = intel?.origin_location?.label || formatPricingLocationScope(intel);
  const closest = intel?.closest_award_label;
  let text = `Source awards near ${origin} — each row shows $/sq ft per visit used in the recommended bid.`;
  if (closest) {
    text += ` Nearest match: ${closest}.`;
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
      <tr><th>Distance</th><th>Work location</th><th>Sq ft</th><th>Frequency</th><th>$/sq ft/visit</th><th>Award date</th><th>Recipient</th><th>Amount</th><th>Agency</th></tr>
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
          <td>${escapeHtml(formatSqFt(a.award_square_feet))}</td>
          <td>${escapeHtml(a.award_frequency_label || "—")}</td>
          <td>${escapeHtml(formatUnitRate(a.price_per_sqft_per_visit))}</td>
          <td>${escapeHtml(formatAwardDate(a))}</td>
          <td>${escapeHtml(a.recipient_name || "—")}</td>
          <td>${formatMoney(a.award_amount)}</td>
          <td>${escapeHtml(a.awarding_agency || "—")}</td>
        </tr>`).join("")}
    </tbody>
  </table>`;
}

function renderPricingBidHero(intel) {
  const annualBid = recommendedAnnualBid(intel);
  const bidFormula = recommendedBidFormula(intel);
  if (!annualBid) {
    return `<p class="pricing-note pricing-note-muted">${escapeHtml(intel.recommended_bid_note || "Recommended annual bid will appear once square footage, frequency, and rated comparables are available.")}</p>`;
  }
  return `
    <div class="pricing-bid-hero">
      <span class="pricing-bid-label">Recommended annual bid</span>
      <span class="pricing-bid-range">${formatMoney(annualBid)}</span>
    </div>
    ${bidFormula ? `<p class="pricing-note pricing-formula">${escapeHtml(bidFormula)}</p>` : ""}
    <p class="pricing-note pricing-note-muted">Initial interest only — regional avg $/sq ft per visit × your sq ft × annual visits. See table for source awards.</p>`;
}

function renderPricingSupportingStats(intel, { winner } = {}) {
  const ur = intel?.unit_rate_summary;
  const winnerLabel = winner ?? (intel.most_frequent_winner
    ? `${intel.most_frequent_winner}${intel.most_frequent_winner_count > 1 ? ` (score ${intel.most_frequent_winner_count})` : ""}`
    : "—");
  if (!ur?.regional_avg_price_per_sqft_per_visit) {
    return winnerLabel !== "—"
      ? `<div class="pricing-stats"><div class="pricing-stat pricing-stat-wide"><span class="pricing-stat-label">Most frequent winner</span><span class="pricing-stat-value pricing-stat-text">${escapeHtml(winnerLabel)}</span></div></div>`
      : "";
  }
  return `
    <div class="pricing-stats">
      <div class="pricing-stat pricing-stat-highlight">
        <span class="pricing-stat-label">Regional avg (drives bid)</span>
        <span class="pricing-stat-value">${escapeHtml(formatUnitRate(ur.regional_avg_price_per_sqft_per_visit))}</span>
      </div>
      <div class="pricing-stat">
        <span class="pricing-stat-label">$/sq ft/visit range</span>
        <span class="pricing-stat-value">${escapeHtml(formatUnitRate(ur.lowest_price_per_sqft_per_visit))} – ${escapeHtml(formatUnitRate(ur.highest_price_per_sqft_per_visit))}</span>
      </div>
      <div class="pricing-stat">
        <span class="pricing-stat-label">Rated comparables</span>
        <span class="pricing-stat-value">${ur.rated_awards_count ?? "—"} of ${intel.awards_count ?? "—"}</span>
      </div>
      <div class="pricing-stat pricing-stat-wide">
        <span class="pricing-stat-label">Most frequent winner</span>
        <span class="pricing-stat-value pricing-stat-text">${escapeHtml(winnerLabel)}</span>
      </div>
    </div>`;
}

function renderClaudePricingPanel(pricing, rawIntel) {
  const annualBid = recommendedAnnualBid(rawIntel);
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
    ? `${rawIntel.awards_count} local comparables in ${locationScope}${recentCount != null ? ` · ${recentCount} in last 12 months` : ""} · NAICS ${rawIntel.naics_code || ""}`
    : "Based on USAspending.gov historical data in the same work area";

  const awardsTable = renderAwardsTable(rawIntel?.awards, rawIntel);

  return `
    <div class="pricing-panel">
      ${renderPricingBidHero(rawIntel)}
      <p class="pricing-intro">${escapeHtml(awardsNote)} <span class="pricing-source">(table = source data · bid = unit-rate formula)</span></p>
      ${rawIntel?.location_scope_note ? `<p class="pricing-note">${escapeHtml(rawIntel.location_scope_note)}</p>` : ""}
      ${awardsTable}
      ${renderPricingSupportingStats(rawIntel, { winner })}
      ${pricing.pricing_summary ? `<p class="pricing-summary">${escapeHtml(pricing.pricing_summary)}</p>` : ""}
      <div class="pricing-stats">
        <div class="pricing-stat">
          <span class="pricing-stat-label">Incumbent</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(incumbent)}</span>
        </div>
        <div class="pricing-stat">
          <span class="pricing-stat-label">Competition</span>
          <span class="pricing-stat-value pricing-stat-text">${escapeHtml(competition)} · ${escapeHtml(confidence)}</span>
        </div>
      </div>
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
      ${renderPricingBidHero(intel)}
      <p class="pricing-intro">
        ${intel.awards_with_dates || intel.awards_count} local contract${(intel.awards_with_dates || intel.awards_count) === 1 ? "" : "s"} near
        <strong>${escapeHtml(locationScope)}</strong> · NAICS
        <strong>${escapeHtml(intel.naics_code || "")}</strong>
        ${intel.awards_last_12_months != null ? ` · ${intel.awards_last_12_months} in last 12 months` : ""}
        <span class="pricing-source">(table = source awards · recommended bid = unit-rate formula only)</span>
      </p>
      ${intel.location_scope_note ? `<p class="pricing-note">${escapeHtml(intel.location_scope_note)}</p>` : ""}
      ${awardsTable}
      ${renderPricingSupportingStats(intel, { winner })}
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

function formatScreenBudget(budget) {
  if (!budget) return "";
  const used = budget.screens_used_today ?? 0;
  if (budget.screens_unlimited || budget.screen_daily_limit === 0) {
    return `Claude screens: ${used} today (no daily cap)`;
  }
  return `Claude screens: ${used}/${budget.screen_daily_limit} (${budget.screens_remaining} left)`;
}

function formatApiBudget(budget) {
  if (!budget) return "";
  return `SAM.gov: ${budget.sam_used_today}/${budget.sam_daily_limit} used (${budget.sam_remaining} left) · ${formatScreenBudget(budget)}`;
}

async function runSync({ allNaics = false, searchOnly = false } = {}) {
  const buttonIds = ["sync-all-btn", "refresh-btn", "search-only-btn"];
  const buttons = buttonIds.map((id) => document.getElementById(id)).filter(Boolean);
  const activeBtn = allNaics
    ? document.getElementById("sync-all-btn")
    : searchOnly
      ? document.getElementById("search-only-btn")
      : document.getElementById("refresh-btn");
  const savedLabels = Object.fromEntries(buttons.map((b) => [b.id, b.textContent]));
  buttons.forEach((b) => {
    b.disabled = true;
  });
  if (activeBtn) activeBtn.textContent = "Syncing...";

  const naics = selectedSyncNaics();
  if (allNaics) {
    showSyncStatus(
      `Pulling all ${config.naics_codes?.length || 6} NAICS codes, then reading descriptions + attachments and writing summaries…`
    );
  } else if (searchOnly) {
    showSyncStatus(
      `Searching SAM.gov for NAICS ${naics || config.naics_sync?.next_naics || "next in rotation"} (1 API call only)…`
    );
  } else {
    showSyncStatus("Searching SAM.gov, then reading descriptions + attachments and writing summaries for matching bids…");
  }

  try {
    const params = new URLSearchParams();
    if (allNaics) {
      params.set("all_naics", "true");
    } else {
      if (naics) params.set("naics", naics);
      if (searchOnly) params.set("search_only", "true");
    }
    const url = `/api/sync?${params.toString()}`;
    const res = await apiFetch(url, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Sync failed");
    const budgetLine = data.api_budget ? ` ${formatApiBudget(data.api_budget)}` : "";
    const intake = data.intake || {};
    const intakeLine =
      searchOnly || !intake.screened
        ? searchOnly
          ? " Open a contract card to test PIEE attachments and summaries."
          : intake.processed
            ? ` Processed ${intake.processed} (descriptions loaded; summaries may still be running in background).`
            : ""
        : ` Wrote summaries for ${intake.screened} contract(s).`;
    const attachLine =
      !searchOnly && data.scrape?.scraped_complete
        ? ` Scraped ${data.scrape.scraped_complete} complete (all attachments).`
        : !searchOnly && data.attachments?.attachments_enriched
          ? ` Loaded attachments on ${data.attachments.attachments_enriched} contract(s).`
          : "";
    const skippedLine =
      data.scrape?.scraped_skipped
        ? ` ${data.scrape.scraped_skipped} incomplete (SAM budget — retry tomorrow or raise SAM_DAILY_API_BUDGET).`
        : "";
    showSyncStatus(`${data.fetch_status} Saved ${data.new} new, ${data.updated} updated.${attachLine}${skippedLine}${intakeLine}${budgetLine}`);
    await loadConfig();
    await loadContracts();
  } catch (err) {
    if (err.message !== "Login required") showSyncStatus(err.message, true);
  } finally {
    buttons.forEach((b) => {
      b.disabled = false;
      b.textContent = savedLabels[b.id];
    });
  }
}

async function runScreen() {
  const btn = document.getElementById("screen-btn");
  btn.disabled = true;
  btn.textContent = "Screening...";
  showSyncStatus("Analyzing matching contracts (reads PDF attachments — may take several minutes)...");
  try {
    const res = await apiFetch("/api/screen?limit=5&matching_only=true", { method: "POST" });
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

  const subSearch = data.sub_search || {};
  document.getElementById("settings-sub-radius").value = String(subSearch.search_radius_miles || 25);
  document.getElementById("settings-sub-min-rating").value = subSearch.min_rating ?? 3.5;
  document.getElementById("settings-sub-min-reviews").value = subSearch.min_review_count ?? 5;

  const keys = data.api_keys || {};
  document.getElementById("api-key-status").innerHTML = `
    <li>SAM.gov: ${keys.sam_gov ? "configured" : "missing"}</li>
    <li>Anthropic: ${keys.anthropic ? "configured" : "missing"}</li>
    <li>Google Places: ${keys.google_places ? "configured" : "missing"}</li>
    <li>PostgreSQL: ${keys.database ? "configured" : "missing"}</li>
  `;

  const budget = data.api_budget || {};
  const screenLine = budget.screens_unlimited || budget.screen_daily_limit === 0
    ? `${budget.screens_used_today ?? 0} used today (no daily cap)`
    : `${budget.screens_used_today ?? 0} / ${budget.screen_daily_limit ?? "?"} used today (${budget.screens_remaining ?? "?"} remaining)`;
  document.getElementById("api-budget-status").innerHTML = `
    <li>SAM.gov API (search/enrich): ${budget.sam_used_today ?? 0} / ${budget.sam_daily_limit ?? "?"} used today (${budget.sam_remaining ?? "?"} remaining)</li>
    <li>SAM.gov PDF downloads (for Claude): ${budget.sam_pdf_downloads_today ?? 0} / ${budget.sam_pdf_download_limit ?? "?"} used today (${budget.sam_pdf_downloads_remaining ?? "?"} remaining)</li>
    <li>Claude screenings: ${screenLine}</li>
    <li>Full intake on sync: ${budget.intake_on_sync !== false ? "on" : "off"} (up to ${budget.intake_per_sync_limit ?? 5} Claude summaries per sync)</li>
    <li>Contract scrape: every sync pulls SAM attachments + PIEE file lists before saving</li>
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
      sub_search_radius_miles: Number(document.getElementById("settings-sub-radius").value),
      sub_min_rating: Number(document.getElementById("settings-sub-min-rating").value),
      sub_min_review_count: Number(document.getElementById("settings-sub-min-reviews").value),
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
document.getElementById("search-only-btn").addEventListener("click", () => runSync({ searchOnly: true }));
document.getElementById("refresh-btn").addEventListener("click", () => runSync({ searchOnly: false }));
document.getElementById("sync-all-btn").addEventListener("click", () => runSync({ allNaics: true }));
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
