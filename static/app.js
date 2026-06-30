let config = { naics_codes: [], naics_labels: {}, naics_tiers: [], naics_groups: [], all_naics_codes: [], default_min_days: 10, default_min_score: 1 };
let contracts = [];
let processingCount = 0;
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
  document.getElementById("view-pricing").hidden = name !== "pricing";
  document.getElementById("view-proposal-subs").hidden = name !== "proposal-subs";
  document.getElementById("view-proposal-config").hidden = name !== "proposal-config";
  document.getElementById("view-proposal-editor").hidden = name !== "proposal-editor";
  document.getElementById("tab-dashboard").classList.toggle("active", name === "dashboard");
  document.getElementById("tab-subs").classList.toggle("active", name === "subs");
  document.getElementById("tab-pricing").classList.toggle("active", name === "pricing");
  document.getElementById("tab-settings").classList.toggle("active", name === "settings");
  if (name === "settings") loadSettingsPage();
}

function renderNaicsFilters() {
  const labels = config.naics_labels || {};
  const container = document.getElementById("naics-filters");
  if (!container) return;
  const activeCodes = config.naics_codes || [];
  container.innerHTML = activeCodes
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

function renderNaicsSettingsToggles(activeCodes) {
  const container = document.getElementById("settings-naics-toggles");
  if (!container) return;
  const labels = config.naics_labels || {};
  const active = new Set(activeCodes || []);
  const tiers = config.naics_tiers || config.naics_groups || [];
  if (!tiers.length) {
    container.innerHTML = (config.all_naics_codes || config.naics_codes || [])
      .map(
        (code) => `
      <label class="naics-settings-toggle">
        <input type="checkbox" class="settings-naics-toggle" value="${code}" ${active.has(code) ? "checked" : ""}>
        <span class="naics-filter-label">${escapeHtml(code)}</span>
        <span class="naics-filter-desc">${escapeHtml(labels[code] || "Other Services")}</span>
      </label>`
      )
      .join("");
    return;
  }
  container.innerHTML = tiers
    .map(
      (group) => `
    <div class="naics-settings-group naics-tier-group">
      <h4>${escapeHtml(group.name || `Tier ${group.tier}`)}</h4>
      <p class="filter-help tier-schedule-help">${escapeHtml(group.schedule || "")}</p>
      ${(group.codes || [])
        .map(
          (code) => `
        <label class="naics-settings-toggle">
          <input type="checkbox" class="settings-naics-toggle" value="${code}" ${active.has(code) ? "checked" : ""}>
          <span class="naics-filter-label">${escapeHtml(code)}</span>
          <span class="naics-filter-desc">${escapeHtml(labels[code] || "Other Services")}</span>
        </label>`
        )
        .join("")}
    </div>`
    )
    .join("");
}

function tierBadge(c) {
  if (!c.tier) return "";
  const label = c.tier_label || `Tier ${c.tier}`;
  return `<span class="badge badge-tier badge-tier-${c.tier}">${escapeHtml(label)}</span>`;
}

function selectedSettingsNaics() {
  return [...document.querySelectorAll(".settings-naics-toggle:checked")].map((el) => el.value);
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
  const tierSchedule = config.naics_tier_schedule || "Tier 1 daily · Tier 2 Mon/Wed/Fri · Tier 3 Sunday";
  const batchSize = sync.scheduled_batch_size || sync.scheduled_per_sync || 1;
  const nextBatch = (sync.scheduled_next_batch || []).join(", ");
  const poolSize = sync.scheduled_pool_size || sync.total_count || config.naics_codes.length;
  const focusLine = sync.focus_naics
    ? ` · focus: ${sync.focus_naics}${sync.focus_pending_attachments ? ` (${sync.focus_pending_attachments} attachments pending)` : ""}`
    : "";
  const scheduledToday = sync.scheduled_tiers
    ? ` · today: tiers ${sync.scheduled_tiers.join(", ")} (${poolSize} in pool, 1 NAICS/run${nextBatch ? ` · next search: ${nextBatch}` : ""})`
    : "";
    document.getElementById("naics-sync-status").textContent =
    `${tierSchedule}. Uses all ${config.api_budget?.sam_daily_limit ?? 10} SAM API calls daily: finish current NAICS attachments, then search/enrich the next until budget is gone.${scheduledToday}${focusLine} Coverage: ${sync.synced_count || 0}/${sync.total_count || config.naics_codes.length} enabled codes synced${nextNaics ? ` · manual rotation: ${nextNaics}` : ""}`;

  populateSyncNaicsSelect();
  renderNaicsFilters();
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
  if (naics.length) {
    params.set("naics", naics.join(","));
  } else {
    params.set("naics", "__none__");
  }
  params.set("min_days", document.getElementById("min-days").value);
  params.set("min_score", document.getElementById("min-score").value);
  const agency = document.getElementById("agency-filter").value.trim();
  if (agency) params.set("agency", agency);
  if (document.getElementById("pursue-only").checked) params.set("pursue_only", "true");
  const tier = document.getElementById("tier-filter")?.value;
  if (tier) params.set("tier", tier);
  return params.toString();
}

function cardTone(c) {
  const stage = c.screening_stage;
  const textScore = c.text_score ?? c.score;
  if (stage === "text" && textScore != null && textScore < 6) return "skip";
  if (stage === "text" && !c.plain_english_summary) return "text-pending";
  if (c.pursue === true) return "pursue";
  if (c.pursue === false) return "skip";
  if (c.score != null && c.score >= 5 && c.score <= 7) return "maybe";
  return "unscreened";
}

function screeningBadge(c) {
  const tone = cardTone(c);
  const stage = c.screening_stage;
  const textScore = c.text_score ?? c.score;
  if (stage === "text" && textScore != null) {
    if (textScore < 6 || c.skip_reason) {
      const label = c.skip_reason || "Low score";
      return `<span class="badge badge-skip">Text ${textScore}/10 · ${escapeHtml(label)}</span>`;
    }
    if (!c.plain_english_summary) {
      return `<span class="badge badge-text-pending">Text ${textScore}/10 · Awaiting full analysis</span>`;
    }
  }
  if (tone === "pursue") return `<span class="badge badge-pursue">Pursue${c.score != null ? ` · ${c.score}/10` : ""}</span>`;
  if (tone === "skip") return `<span class="badge badge-skip">Skip${c.score != null ? ` · ${c.score}/10` : ""}</span>`;
  if (tone === "maybe") return `<span class="badge badge-maybe">Maybe · ${c.score}/10</span>`;
  if (tone === "text-pending") return `<span class="badge badge-text-pending">Text ${textScore ?? "?"}/10</span>`;
  return `<span class="badge badge-pending">Not screened</span>`;
}

function renderPipelineStrip(c) {
  const pipe = c.pipeline || {};
  const steps = [...(pipe.intake || []), ...(pipe.bid || [])];
  if (!steps.length) return "";
  const locked = pipe.do_not_rebid;
  let currentIdx = steps.findIndex((s) => s.state === "pending");
  if (currentIdx === -1) currentIdx = steps.length - 1;

  return `
    <div class="workflow-track${locked ? " workflow-track-locked" : ""}" aria-label="Contract progress">
      ${locked ? `<p class="workflow-track-lock">Already submitted — do not bid again</p>` : ""}
      <ol class="workflow-stepper">
        ${steps
          .map((s, i) => {
            let stepClass = "upcoming";
            if (s.state === "done") stepClass = "done";
            else if (i === currentIdx) stepClass = "current";
            return `<li class="workflow-step workflow-step-${stepClass}">
              <span class="workflow-step-marker" aria-hidden="true"></span>
              <span class="workflow-step-label">${escapeHtml(s.label)}</span>
            </li>`;
          })
          .join("")}
      </ol>
    </div>`;
}

function renderCardActions(c) {
  const wf = c.workflow || {};
  const pursueBtn = typeof renderPursueButton === "function" ? renderPursueButton(c) : "";
  const continueProposal = typeof renderContinueProposal === "function" ? renderContinueProposal(c) : "";
  const findSubsBtn = typeof renderFindSubsButton === "function" ? renderFindSubsButton(c) : "";
  const forceFullBtn = renderForceFullAnalysisButton(c);
  const primary = continueProposal || pursueBtn;
  const secondary = [findSubsBtn, forceFullBtn].filter(Boolean).join("");
  if (!primary && !secondary) return "";
  return `
    <div class="card-actions">
      <span class="card-label">Next step</span>
      <div class="card-actions-row">
        ${primary ? `<div class="card-actions-primary">${primary}</div>` : ""}
        ${secondary ? `<div class="card-actions-secondary">${secondary}</div>` : ""}
      </div>
      ${wf.label && c.pursue === true ? `<p class="card-actions-hint">${escapeHtml(wf.label)}</p>` : ""}
    </div>`;
}

function renderSolicitationMetaSection(c) {
  const analysis = c.analysis || {};
  const sol = analysis.solicitation_meta || {};
  const start = sol.base_year_start || analysis.base_year_start || "—";
  const end = sol.base_year_end || analysis.base_year_end || "—";
  const co = sol.contracting_officer_name || analysis.contracting_officer_name || "—";
  return `
    <p class="detail-item"><span class="detail-item-label">Performance period</span> ${escapeHtml(start)} – ${escapeHtml(end)}</p>
    <p class="detail-item"><span class="detail-item-label">Contracting Officer</span> ${escapeHtml(co)}</p>
    <button type="button" class="btn btn-secondary-action btn-small" id="extract-solicitation-btn" data-notice-id="${escapeHtml(c.notice_id)}">Extract scope &amp; solicitation from PDFs</button>
    <p class="detail-note">Reads attachments (including large PDFs) — fills PWS scope, dates, CO, proposals, and subcontract agreements.</p>`;
}

function renderForceFullAnalysisButton(c) {
  if (c.screening_stage === "full") return "";
  return `<button type="button" class="btn btn-secondary-action btn-small card-force-full" data-force-full="${escapeHtml(c.notice_id)}">Force Full Analysis</button>`;
}

async function forceFullAnalysis(noticeId) {
  showSyncStatus("Downloading PDFs and running full analysis…");
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/full-analysis`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Full analysis failed");
    showSyncStatus("Full analysis complete.");
    await loadContracts();
    if (activeDetailId === noticeId) openDetail(noticeId);
  } catch (err) {
    showSyncStatus(err.message, true);
  }
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

function formatScopePreview(c) {
  const sqft = c.square_footage ?? c.pws?.square_footage;
  const freq = c.cleaning_frequency_per_week ?? c.pws?.cleaning_frequency_per_week;
  if (!sqft && freq == null) return "";
  const parts = [];
  if (sqft) parts.push(`${Number(sqft).toLocaleString()} sq ft`);
  if (freq != null) {
    const n = Number(freq);
    parts.push(n === 5 ? "M–F" : n === 7 ? "Daily" : `${n}×/week`);
  }
  return `
    <div class="card-section card-section-scope card-section-compact">
      <span class="card-label">Scope</span>
      <p class="card-meta card-scope">${escapeHtml(parts.join(" · "))}</p>
    </div>`;
}

function subcontractingLimitationBadge(c) {
  const check = c.subcontracting_limitation_check;
  if (!check) return "";
  if (check === "NOT_FOUND") {
    return `<span class="badge badge-subcontract-ok">No Subcontracting Limit Found</span>`;
  }
  if (check === "FOUND") {
    const pct = c.subcontracting_limitation_percentage;
    const pctLabel = pct != null ? ` (${pct}%)` : "";
    return `<span class="badge badge-subcontract-found">⚠ SUBCONTRACTING LIMIT PRESENT — REVIEW BEFORE BIDDING${escapeHtml(pctLabel)}</span>`;
  }
  if (check === "EXTRACTION_FAILED") {
    return `<span class="badge badge-subcontract-unverified">⚠ Could Not Verify — Manual Check Required</span>`;
  }
  return "";
}

function renderSubcontractingComplianceBanner(c) {
  const check = c.subcontracting_limitation_check;
  if (c.pursue !== true) return "";
  if (check !== "FOUND" && check !== "EXTRACTION_FAILED") return "";
  const context =
    check === "FOUND" && c.subcontracting_limitation_context
      ? `<p class="compliance-banner-context">${escapeHtml(c.subcontracting_limitation_context)}</p>`
      : "";
  return `<div class="compliance-banner compliance-banner-${check === "FOUND" ? "found" : "unverified"}">
    <strong>Subcontracting compliance warning</strong>
    <p>This contract may limit how much work can be subcontracted, or this could not be verified. Confirm manually before submitting a bid — this directly affects whether Northern RE Investments LLC's business model is compliant for this contract.</p>
    ${context}
  </div>`;
}

function renderCards() {
  const container = document.getElementById("cards");
  const proc = processingCount > 0 ? ` · ${processingCount} processing (attachments + PDF read)` : "";
  document.getElementById("results-count").textContent =
    `${contracts.length} contract${contracts.length === 1 ? "" : "s"}${proc}`;

  if (!contracts.length) {
    container.innerHTML =
      processingCount > 0
        ? `<div class="empty">${processingCount} contract${processingCount === 1 ? "" : "s"} downloading attachments and reading PDFs — they will appear here automatically when ready. Nothing to click.</div>`
        : '<div class="empty">No contracts match your filters yet.</div>';
    return;
  }

  container.innerHTML = contracts.map((c) => {
    const tone = cardTone(c);
    const due = formatDue(c);
    const subType = c.sub_type_needed || "Not screened yet";
    const summary = c.plain_english_summary || c.executive_summary;
    const headline = summary ? firstSentence(summary, 160) : null;
    const annualBid = recommendedAnnualBid(c);
    const bidLabel = c.selected_sub_quote ? "Your estimated bid" : "Recommended annual bid";
    const bidPreview = annualBid
      ? `<div class="card-section card-section-bid">
           <span class="card-label">${bidLabel}</span>
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
           ${c.text_score != null && c.screening_stage === "text" && !c.plain_english_summary
             ? `<p class="card-pending-note">Text score ${c.text_score}/10 — full PDF analysis pending</p>`
             : `<p class="card-pending-note">Plain-English summary being generated…</p>`}
         </div>`;
    const naicsLine = c.naics_display || c.naics_code || "";
    const attachmentsHtml = renderCardAttachments(c);
    const networkBanner = typeof renderNetworkBanner === "function" ? renderNetworkBanner(c) : "";
    const subSummaryCard = typeof renderCardSubSummary === "function" ? renderCardSubSummary(c) : "";
    const wf = c.workflow || {};
    const workflowClass = wf.stage && wf.stage !== "none" ? ` card-workflow-${wf.stage}` : "";
    const workflowBanner = typeof renderWorkflowBanner === "function" ? renderWorkflowBanner(c) : "";
    const pipelineStrip = renderPipelineStrip(c);
    const cardActions = renderCardActions(c);
    return `
    <article class="card card-${tone}${workflowClass}" data-id="${c.notice_id}">
      <div class="card-top">
        <div class="card-badges">${screeningBadge(c)} ${subcontractingLimitationBadge(c)} ${tierBadge(c)} ${wf.label ? `<span class="badge badge-workflow">${escapeHtml(wf.label)}</span>` : ""} ${c.security_clearance_required ? '<span class="badge badge-clearance">Clearance</span>' : ""}</div>
        <div class="card-due${due.urgent ? " card-due-urgent" : ""}">
          <span class="card-due-label">Due</span>
          <span class="card-due-date">${escapeHtml(due.main)}</span>
          ${due.sub ? `<span class="card-due-days">${escapeHtml(due.sub)}</span>` : ""}
        </div>
      </div>
      ${pipelineStrip}
      ${workflowBanner}
      <div class="card-body">
        <div class="card-main">
          ${titleBlock}
          ${networkBanner}
          ${subSummaryCard}
        </div>
        <aside class="card-side">
          ${bidPreview}
          ${formatScopePreview(c)}
          <div class="card-section card-section-location card-section-compact">
            <span class="card-label">Where</span>
            <p class="card-meta">${escapeHtml(c.location || "Location unknown")}</p>
            <p class="card-meta card-agency">${escapeHtml(c.agency || "Unknown agency")}</p>
            <p class="card-meta card-naics"><span class="card-label-inline">NAICS</span> ${escapeHtml(naicsLine)}</p>
            <p class="card-subtype"><span class="card-label-inline">Sub type</span> ${escapeHtml(subType)}</p>
          </div>
        </aside>
      </div>
      ${attachmentsHtml}
      ${cardActions}
    </article>`;
  }).join("");

  container.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select, textarea")) return;
      openDetail(el.dataset.id);
    });
  });
  container.querySelectorAll("[data-force-full]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      forceFullAnalysis(btn.dataset.forceFull);
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
  const attachmentsPending = !(c.sam_attachments || []).length && c.screening_stage === "full";
  const textPendingFull =
    c.screening_stage === "text" && !c.plain_english_summary;
  const notScreened = !c.analysis && !c.text_score;
  return subsSearching || attachmentsPending || textPendingFull || notScreened;
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
  processingCount = data.processing_count || 0;
  renderCards();
  manageCardPolling();
}

async function loadContractsQuiet() {
  const res = await apiFetch(`/api/contracts?${buildQuery()}`);
  const data = await res.json();
  contracts = data.contracts || [];
  processingCount = data.processing_count || 0;
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
  const pricingInner = `<div id="pricing-panel" class="pricing-panel pricing-panel-loading">
         <p class="pricing-loading">Loading pricing intelligence…</p>
       </div>`;
  const subsLink = typeof renderSubSummaryLink === "function" ? renderSubSummaryLink(c) : "";
  const pursueSection = typeof renderPursueSection === "function" ? renderPursueSection(c) : "";
  const solSection = renderSolicitationMetaSection(c);
  const pipelineStrip = renderPipelineStrip(c);

  document.getElementById("modal-content").innerHTML = `
    <div class="detail-header">
      <h2 class="detail-title">${escapeHtml(c.title)}</h2>
      <p class="detail-agency">${escapeHtml(c.agency || "Unknown agency")}</p>
    </div>
    ${renderSubcontractingComplianceBanner(c)}
    ${pipelineStrip}
    <div class="detail-workflow-grid">
      ${wrapDetailSection("1 · Evaluate", summaryInner, "detail-section-summary")}
      ${wrapDetailSection("2 · Solicitation", solSection, "detail-section-solicitation")}
      ${wrapDetailSection("3 · Pricing", pricingInner, "detail-section-pricing")}
      ${wrapDetailSection("4 · Subs", subsLink || "<p>Run Find Subs to search Google Places.</p>", "detail-section-subs")}
      ${wrapDetailSection("5 · Pursue", pursueSection, "detail-section-pursue")}
    </div>
  `;
  document.getElementById("extract-solicitation-btn")?.addEventListener("click", () => {
    extractSolicitationMeta(c.notice_id).catch((err) => showSyncStatus(err.message, true));
  });
}

async function extractSolicitationMeta(noticeId) {
  showSyncStatus("Extracting scope and solicitation details from PDFs…");
  const res = await apiFetch(
    `/api/contracts/${encodeURIComponent(noticeId)}/extract-solicitation?force=true`,
    { method: "POST" },
  );
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Extraction failed");
  showSyncStatus("Scope and solicitation details updated.");
  await loadContracts();
  if (activeDetailId === noticeId) {
    openDetail(noticeId);
    if (typeof loadPricingIntel === "function") loadPricingIntel(noticeId);
  }
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
        loadPricingIntel(noticeId);
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
      loadPricingIntel(noticeId);
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
  renderDetailModal(c, { analyzing: !summary });
  document.getElementById("modal").hidden = false;
  loadPricingIntel(noticeId);

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

function mergeContractUpdate(updated) {
  if (!updated?.notice_id) return;
  const i = contracts.findIndex((c) => c.notice_id === updated.notice_id);
  if (i >= 0) contracts[i] = { ...contracts[i], ...updated };
}

function recommendedAnnualBid(c) {
  const quote = c?.selected_sub_quote;
  const margin = c?.effective_margin_pct ?? c?.margin_percentage ?? 20;
  if (quote && quote > 0) {
    return quote / (1 - margin / 100);
  }
  if (c?.estimated_annual_bid) return c.estimated_annual_bid;
  const intel = c?.pricing_intel;
  if (intel?.internal?.recommended_annual_bid) return intel.internal.recommended_annual_bid;
  return null;
}

function formatUnitRate(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  if (n >= 1) return `$${n.toFixed(2)}/sq ft/visit`;
  if (n >= 0.01) return `$${n.toFixed(3)}/sq ft/visit`;
  return `$${n.toFixed(4)}/sq ft/visit`;
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

function formatApiBudget(budget) {
  if (!budget) return "";
  return `SAM.gov API: ${budget.sam_used_today}/${budget.sam_daily_limit} used (${budget.sam_remaining} left today)`;
}

async function runSync({ allNaics = false, searchOnly = false } = {}) {
  const buttonIds = ["search-all-btn", "refresh-btn", "search-only-btn"];
  const buttons = buttonIds.map((id) => document.getElementById(id)).filter(Boolean);
  const activeBtn = allNaics
    ? document.getElementById("search-all-btn")
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
    const enabledCount = config.naics_codes?.length || 0;
    if (!enabledCount) {
      showSyncStatus("No NAICS codes enabled. Turn on codes in Settings → Search.", true);
      buttons.forEach((b) => {
        b.disabled = false;
        b.textContent = savedLabels[b.id];
      });
      return;
    }
    showSyncStatus(
      `Search All — ${enabledCount} enabled code(s) across all tiers, then reading descriptions + attachments…`
    );
  } else if (searchOnly) {
    showSyncStatus(
      `Searching SAM.gov for NAICS ${naics || config.naics_sync?.next_naics || "next in rotation"} (1 API call only)…`
    );
  } else {
    showSyncStatus("Searching SAM.gov — saves all filter-matching contracts, then pulls attachments and runs Claude analysis when ready…");
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
      !searchOnly && (data.scrape?.attachments_enriched ?? data.attachments?.attachments_enriched)
        ? ` Attachments ready on ${data.scrape?.attachments_enriched ?? data.attachments?.attachments_enriched} contract(s) this run.`
        : "";
    const skippedLine =
      data.scrape?.attachments_pending
        ? ` ${data.scrape.attachments_pending} still waiting on attachments (retried next sync).`
        : data.scrape?.scraped_skipped
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

async function saveBlobWithPicker(blob, filename, mimeType = "application/json") {
  if (typeof window.showSaveFilePicker === "function") {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [
          {
            description: "JSON export",
            accept: { [mimeType]: [".json"] },
          },
        ],
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return handle.name;
    } catch (err) {
      if (err?.name === "AbortError") {
        throw err;
      }
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return filename;
}

async function exportForClaude() {
  const btn = document.getElementById("export-claude-btn");
  const statusEl = document.getElementById("export-claude-status");
  if (!btn) return;
  btn.disabled = true;
  if (statusEl) statusEl.textContent = "Building full export — contracts, subs, pricing, and attachment text…";
  try {
    const res = await apiFetch("/api/export/claude");
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Export failed");
    }
    const blob = await res.blob();
    const disp = res.headers.get("Content-Disposition") || "";
    const match = disp.match(/filename=\"?([^\";]+)\"?/i);
    const filename = match ? match[1] : `govtracker-claude-export-${new Date().toISOString().slice(0, 10)}.json`;
    const savedAs = await saveBlobWithPicker(blob, filename);
    if (statusEl) statusEl.textContent = `Saved ${savedAs}`;
    showSyncStatus(`Exported ${savedAs}`);
  } catch (err) {
    if (err?.name === "AbortError") {
      if (statusEl) statusEl.textContent = "Export cancelled.";
      return;
    }
    const msg = err.message || "Export failed";
    if (statusEl) statusEl.textContent = msg;
    showSyncStatus(msg, true);
  } finally {
    btn.disabled = false;
  }
}

async function loadSettingsPage() {
  const res = await apiFetch("/api/settings");
  const data = await res.json();
  config.naics_codes = data.naics_codes || [];
  config.all_naics_codes = data.all_naics_codes || config.naics_codes;
  config.naics_tiers = data.naics_tiers || data.naics_groups || config.naics_tiers || [];
  config.naics_groups = data.naics_groups || config.naics_groups || [];
  config.naics_tier_schedule = data.naics_tier_schedule || config.naics_tier_schedule || "";
  config.naics_labels = data.naics_labels || config.naics_labels || {};
  renderNaicsSettingsToggles(data.naics_codes || []);
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

  const owner = data.owner || {};
  const set = (id, key) => { const el = document.getElementById(id); if (el) el.value = owner[key] || el.defaultValue || ""; };
  const completion = data.owner_completion || {};
  const missingOwner = completion.missing || [];
  document.getElementById("settings-owner-completion") &&
    (document.getElementById("settings-owner-completion").innerHTML = completion.complete
      ? `<p class="settings-complete">Business profile complete for proposals and agreements.</p>`
      : `<p class="settings-incomplete">Still needed: ${escapeHtml(missingOwner.map((m) => m.label).join(", "))}</p>`);
  document.querySelectorAll("[data-owner-required]").forEach((el) => {
    const key = el.dataset.ownerRequired;
    const empty = !String(owner[key] || "").trim();
    el.classList.toggle("settings-field-missing", empty);
  });
  set("settings-owner-legal", "legal_business_name");
  set("settings-owner-title", "owner_title");
  set("settings-owner-name", "owner_name");
  set("settings-owner-email", "business_email");
  set("settings-owner-phone", "business_phone");
  set("settings-owner-address", "address_line_1");
  set("settings-owner-address2", "address_line_2");
  set("settings-owner-city", "city");
  set("settings-owner-state", "state");
  set("settings-owner-zip", "zip");
  set("settings-owner-uei", "uei");
  set("settings-owner-cage", "cage_code");
  set("settings-owner-ein", "ein");
  if (document.getElementById("settings-owner-sam-exp")) {
    document.getElementById("settings-owner-sam-exp").value = owner.sam_expiration || "";
  }

  const keys = data.api_keys || {};
  document.getElementById("api-key-status").innerHTML = `
    <li>SAM.gov: ${keys.sam_gov ? "configured" : "missing"}</li>
    <li>Anthropic: ${keys.anthropic ? "configured" : "missing"}</li>
    <li>Google Places: ${keys.google_places ? "configured" : "missing"}</li>
    <li>PostgreSQL: ${keys.database ? "configured" : "missing"}</li>
  `;

  const budget = data.api_budget || {};
  document.getElementById("api-budget-status").innerHTML = `
    <li><strong>SAM.gov API</strong> (search + attachment metadata): ${budget.sam_used_today ?? 0} / ${budget.sam_daily_limit ?? "?"} used today — <strong>${budget.sam_remaining ?? "?"} remaining</strong></li>
    <li>Only SAM.gov API calls are capped daily. Claude, PDF reads, and other services are not limited by this app.</li>
    <li>Scheduled 6am sync: uses <strong>all SAM API calls each day</strong> — finish pending attachments, then search/enrich the next NAICS until the budget is exhausted</li>
    <li>Pending attachment pulls resume on the next run until the SAM.gov daily cap is reached</li>
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
  const tierLine = sched.tier_schedule || "Tier 1 daily · Tier 2 Mon/Wed/Fri · Tier 3 Sunday";
  const batchLine = sched.scheduled_per_sync ? ` · ${sched.scheduled_per_sync} code(s)/run` : "";
  const todayLine = sched.scheduled_tiers
    ? ` · pool tiers ${sched.scheduled_tiers.join(", ")} (${sched.scheduled_pool_size || "?"} codes${batchLine})`
    : "";
  if (sched.next_run) {
    const next = new Date(sched.next_run);
    return `${tierLine} · ${hour}:${minute} ${tz} · next ${next.toLocaleString()}${todayLine}`;
  }
  return `${tierLine} · ${hour}:${minute} ${tz}${todayLine}`;
}

function parseSchedulerTime(value) {
  const [hour, minute] = (value || "06:00").split(":");
  return { hour: Number(hour) || 6, minute: Number(minute) || 0 };
}

async function saveSettings() {
  const btn = document.getElementById("save-settings-btn");
  btn.disabled = true;
  try {
    const naics = selectedSettingsNaics();
    if (!naics.length) {
      alert("Select at least one NAICS code.");
      return;
    }
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
    const ownerBody = {
      legal_business_name: document.getElementById("settings-owner-legal")?.value,
      owner_name: document.getElementById("settings-owner-name")?.value,
      owner_title: document.getElementById("settings-owner-title")?.value,
      business_email: document.getElementById("settings-owner-email")?.value,
      business_phone: document.getElementById("settings-owner-phone")?.value,
      address_line_1: document.getElementById("settings-owner-address")?.value,
      address_line_2: document.getElementById("settings-owner-address2")?.value,
      city: document.getElementById("settings-owner-city")?.value,
      state: document.getElementById("settings-owner-state")?.value,
      zip: document.getElementById("settings-owner-zip")?.value,
      uei: document.getElementById("settings-owner-uei")?.value,
      cage_code: document.getElementById("settings-owner-cage")?.value,
      ein: document.getElementById("settings-owner-ein")?.value,
      sam_expiration: document.getElementById("settings-owner-sam-exp")?.value,
    };
    await apiFetch("/api/settings/owner", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ownerBody),
    });
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
document.getElementById("search-all-btn").addEventListener("click", () => runSync({ allNaics: true }));
document.getElementById("screen-btn").addEventListener("click", runScreen);
document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal-backdrop").addEventListener("click", closeModal);
document.getElementById("save-settings-btn").addEventListener("click", saveSettings);
document.getElementById("reset-prompt-btn").addEventListener("click", resetPrompt);
document.getElementById("export-claude-btn")?.addEventListener("click", exportForClaude);

bindSlider("min-days", "min-days-value");
bindSlider("min-score", "min-score-value");
bindSlider("settings-min-days", "settings-min-days-value");
bindSlider("settings-min-score", "settings-min-score-value");

loadConfig().then(loadContracts);
