/** Contract detail page — tabbed full information view. */

let activeContractId = null;
let activeContractTab = "overview";
let activeContractData = null;
let contractDetailPollTimer = null;

const PERF_STATUSES = new Set(["awarded", "active", "option_year", "stop_work", "completed", "won"]);

function isPerformanceContract(c) {
  return PERF_STATUSES.has((c?.status || "").toLowerCase());
}

function contractDetailTabs(c) {
  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "documents", label: "Documents" },
    { id: "subs", label: "Subs" },
    { id: "proposal", label: "Proposal" },
  ];
  if (isPerformanceContract(c)) {
    tabs.push({ id: "performance", label: "Performance" });
  }
  return tabs;
}

async function openContractDetail(noticeId, tab = "overview") {
  activeContractId = noticeId;
  activeContractTab = tab;
  stopContractDetailPolling();
  showView("contract-detail");
  const header = document.getElementById("contract-detail-header");
  if (header) header.innerHTML = `<p class="pricing-loading">Loading contract…</p>`;
  document.getElementById("contract-detail-panels").innerHTML = "";
  try {
    const c = await fetchContract(noticeId);
    if (!c) throw new Error("Contract not found");
    activeContractData = c;
    renderContractDetailShell(c);
    await switchContractTab(tab, c);
    if (getContractSummary(c)) loadContractAdvice(noticeId);
    if (!getContractSummary(c)) beginContractDetailAnalysis(noticeId);
    else startContractDetailPolling(noticeId);
  } catch (err) {
    if (header) header.innerHTML = `<p class="perf-error">${escapeHtml(err.message)}</p>`;
  }
}

function openDetail(noticeId) {
  openContractDetail(noticeId, "overview");
}

async function fetchContract(noticeId) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}`);
  if (!res.ok) return null;
  return res.json();
}

function renderContractDetailShell(c) {
  const sol = c.analysis?.solicitation_meta || {};
  const solNum = sol.solicitation_number || c.notice_id;
  document.getElementById("contract-detail-header").innerHTML = `
    <h2 class="contract-detail-title">${escapeHtml(c.title)}</h2>
    <p class="contract-detail-meta">${escapeHtml(c.agency || "")} · ${escapeHtml(cityStateDisplay(c))} · ${escapeHtml(solNum)}</p>`;

  const tabs = contractDetailTabs(c);
  document.getElementById("contract-detail-tabs").innerHTML = tabs
    .map(
      (t) =>
        `<button type="button" class="contract-tab ${t.id === activeContractTab ? "active" : ""}" data-tab="${t.id}" role="tab">${escapeHtml(t.label)}</button>`,
    )
    .join("");

  document.getElementById("contract-detail-tabs").querySelectorAll(".contract-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchContractTab(btn.dataset.tab, activeContractData));
  });
}

async function switchContractTab(tabId, c) {
  if (!c) c = activeContractData;
  if (!c) return;
  activeContractTab = tabId;
  document.querySelectorAll(".contract-tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tabId);
  });
  const panels = document.getElementById("contract-detail-panels");
  panels.innerHTML = `<div class="contract-tab-panel" id="tab-panel-${tabId}"><p class="pricing-loading">Loading…</p></div>`;
  if (tabId === "overview") renderOverviewTab(c);
  else if (tabId === "documents") renderDocumentsTab(c);
  else if (tabId === "subs") renderSubsTab(c.notice_id);
  else if (tabId === "proposal") renderProposalTab(c);
  else if (tabId === "performance") renderPerformanceTab(c.notice_id);
}

function cityStateDisplay(c) {
  const loc = c.location || "";
  const m = loc.match(/([^,]+),\s*([A-Z]{2})\b/);
  if (m) return `${m[1].trim()}, ${m[2]}`;
  if (c.sub_summary?.city) return c.sub_summary.city;
  const parts = loc.split(",").map((s) => s.trim()).filter(Boolean);
  if (parts.length >= 2) return `${parts[0]}, ${parts[parts.length - 1].slice(0, 2)}`;
  return loc || "—";
}

function renderOverviewTab(c) {
  const panel = document.getElementById("tab-panel-overview");
  const summary = getContractSummary(c) || "Summary not available yet.";
  const pws = c.pws || {};
  const sol = c.analysis?.solicitation_meta || {};
  const solNum = sol.solicitation_number || c.notice_id;
  const pkg = c.submission_package || {};
  const dl = pkg.deadline || {};
  const due = c.due_date
    ? new Date(c.due_date + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
    : "—";
  const popStart = c.period_of_performance_start || "—";
  const popEnd = c.period_of_performance_end || "—";
  const qDeadline = c.questions_deadline || sol.questions_deadline || "—";
  const subMethod = c.submission_method || "Unknown";
  const subEmail = c.submission_email || sol.contracting_officer_email || "";
  const farBadge = renderFarBadge(c);
  const intel = c.pricing_intel || {};
  const pred = intel.predecessor_award || {};
  const incumbent = pred.recipient_name || intel.likely_incumbent || intel.most_frequent_winner || sol.incumbent_contractor || "Not identified";
  const incumbentValue = pred.recent_annual_amount
    ? formatMoney(pred.recent_annual_amount)
    : pred.annual_amount
      ? formatMoney(pred.annual_amount)
      : intel.average_annual_award
        ? formatMoney(intel.average_annual_award)
        : "—";
  const baseYearValue = pred.base_year_amount ? formatMoney(pred.base_year_amount) : "—";
  const priorContract = pred.contract_number || sol.manual_previous_contract_number || sol.previous_contract_number || "—";
  const totalValue = pred.total_value ? formatMoney(pred.total_value) : "—";
  const optionYears = pred.option_years_exercised != null ? String(pred.option_years_exercised) : "—";
  const calcNote = pred.pricing_calc_note || "";
  const modCount = pred.modifications_count != null ? String(pred.modifications_count) : "—";
  const methodLabels = {
    contract_number: "Prior contract (exact #)",
    contract_number_keyword: "Prior contract (exact #)",
    manual_contract_number: "Prior contract (you entered)",
    same_site_match: "Prior contract (same address)",
    facility_keyword: "Prior contract (facility name)",
    recipient_search: "Prior contract (incumbent search)",
    same_city_match: "Prior contract (same city)",
    incumbent_name_match: "Prior contract (incumbent match)",
  };
  const methodLabel = methodLabels[pred.lookup_method] || "Prior contract";
  const incumbentSource = pred.lookup_method
    ? `USAspending — ${methodLabel}`
    : intel.lookup_method
      ? `USAspending (${intel.lookup_method})`
      : intel.source || "USAspending.gov";

  panel.innerHTML = `
    <section class="detail-overview-section">
      <h3>Summary</h3>
      <div class="executive-summary">${formatSummaryHtml(summary)}</div>
      <div id="contract-advice-mount-overview" data-notice-id="${escapeHtml(c.notice_id)}">${renderContractAdviceBox(c)}</div>
    </section>
    <section class="detail-overview-section">
      <h3>Scope</h3>
      <div class="overview-grid">
        <div><span class="card-label">Square footage</span><p>${pws.square_footage ? Number(pws.square_footage).toLocaleString() : c.square_footage || "—"}</p></div>
        <div><span class="card-label">Frequency</span><p>${pws.cleaning_frequency_per_week ?? c.cleaning_frequency_per_week ?? "—"}×/week</p></div>
        <div><span class="card-label">Service type</span><p>${escapeHtml(c.sub_type_needed || c.building_type || "—")}</p></div>
      </div>
    </section>
    <section class="detail-overview-section">
      <h3>Key dates</h3>
      <div class="overview-grid">
        <div><span class="card-label">Due date</span><p>${escapeHtml(due)}${dl.label ? ` · ${escapeHtml(dl.label)}` : ""}</p></div>
        <div><span class="card-label">PoP start</span><p>${escapeHtml(popStart)}</p></div>
        <div><span class="card-label">PoP end</span><p>${escapeHtml(popEnd)}</p></div>
        <div><span class="card-label">Questions deadline</span><p>${escapeHtml(qDeadline)}</p></div>
      </div>
    </section>
    <section class="detail-overview-section">
      <h3>Submission</h3>
      <p><strong>${escapeHtml(subMethod)}</strong>${subEmail ? ` — ${escapeHtml(subEmail)}` : ""}</p>
      ${subEmail ? `<button type="button" class="btn btn-secondary-action btn-small" id="copy-sub-email">Copy email</button>` : ""}
    </section>
    <section class="detail-overview-section">
      <h3>FAR 52.219-14</h3>
      ${farBadge}
      ${c.subcontracting_limitation_context ? `<p class="detail-note">${escapeHtml(c.subcontracting_limitation_context)}</p>` : ""}
    </section>
    <section class="detail-overview-section">
      <h3>Incumbent research</h3>
      ${pred.is_prior_contract
        ? `<p class="pricing-note pricing-incumbent-note"><strong>${escapeHtml(methodLabel)}</strong> — this is what the last contractor was paid, not a regional average.</p>`
        : intel.average_annual_award
          ? `<p class="pricing-note">Showing <strong>regional average</strong> — no prior contract number or incumbent match found in the solicitation yet.</p>`
          : ""}
      <div class="overview-grid">
        <div><span class="card-label">Likely incumbent</span><p>${escapeHtml(incumbent)}</p></div>
        <div><span class="card-label">${pred.is_prior_contract ? "Recent annual pay" : intel.average_annual_award ? "Regional avg (annual)" : "Annual pay"}</span><p>${incumbentValue}</p></div>
        <div><span class="card-label">Base year</span><p>${baseYearValue}</p></div>
        <div><span class="card-label">Prior contract #</span><p>${escapeHtml(priorContract)}</p></div>
        <div><span class="card-label">Total obligated</span><p>${totalValue}</p></div>
        <div><span class="card-label">Option years</span><p>${escapeHtml(optionYears)}</p></div>
        <div><span class="card-label">Modifications</span><p>${escapeHtml(modCount)}</p></div>
        <div><span class="card-label">Source</span><p>${escapeHtml(incumbentSource)}</p></div>
      </div>
      ${calcNote ? `<p class="pricing-note">${escapeHtml(calcNote)}</p>` : ""}
      <div class="prior-contract-lookup">
        <label class="card-label" for="prior-contract-input">Look up prior contract # (USAspending)</label>
        <div class="prior-contract-lookup-row">
          <input id="prior-contract-input" type="text" placeholder="e.g. FA8821-19-F-0123" value="${escapeHtml(sol.manual_previous_contract_number || sol.previous_contract_number || "")}">
          <button type="button" class="btn btn-secondary-action btn-small" id="prior-contract-lookup-btn">Look up</button>
        </div>
        <p class="filter-help">Auto-extracted from solicitation PDFs when present. Paste here only to override or if extraction missed a number.</p>
      </div>
    </section>
    <section class="detail-overview-section">
      <h3>CO questions</h3>
      <div id="co-questions-mount"></div>
    </section>`;

  document.getElementById("copy-sub-email")?.addEventListener("click", () => {
    navigator.clipboard.writeText(subEmail);
    showSyncStatus("Email copied.");
  });
  document.getElementById("prior-contract-lookup-btn")?.addEventListener("click", async () => {
    const input = document.getElementById("prior-contract-input");
    const number = input?.value?.trim();
    if (!number) {
      showSyncStatus("Enter a contract number first.");
      return;
    }
    const btn = document.getElementById("prior-contract-lookup-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await apiFetch(`/api/contracts/${encodeURIComponent(c.notice_id)}/lookup-prior-contract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contract_number: number }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Lookup failed");
      }
      const data = await res.json();
      if (data.contract && typeof openContractDetail === "function") {
        const idx = contracts.findIndex((row) => row.notice_id === c.notice_id);
        if (idx >= 0) contracts[idx] = data.contract;
        openContractDetail(c.notice_id, "overview");
      }
      showSyncStatus("Prior contract pricing updated from USAspending.");
    } catch (err) {
      showSyncStatus(err.message || "Prior contract lookup failed.");
    } finally {
      if (btn) btn.disabled = false;
    }
  });
  const coMount = document.getElementById("co-questions-mount");
  if (coMount && typeof bindCoQuestionsPanel === "function") {
    bindCoQuestionsPanel(coMount, c.notice_id);
  }
}

function renderFarBadge(c) {
  const check = c.subcontracting_limitation_check;
  if (!check) return "";
  if (check === "FOUND") {
    const pct = c.subcontracting_limitation_percentage != null ? ` (${c.subcontracting_limitation_percentage}%)` : "";
    return `<span class="compact-far-badge compact-far-limit" title="FAR 52.219-14 subcontracting limit may apply">SUB LIMIT${escapeHtml(pct)}</span>`;
  }
  if (check === "EXTRACTION_FAILED") {
    return `<span class="compact-far-badge compact-far-unknown" title="Could not verify subcontracting limits from PDFs">VERIFY SUB</span>`;
  }
  if (check === "NOT_FOUND") {
    return `<span class="compact-far-badge compact-far-ok" title="No FAR 52.219-14 self-performance limit found in PDFs">SUB OK</span>`;
  }
  return "";
}

function classifyDocument(att) {
  const name = `${att.description || ""} ${att.filename || ""}`.toLowerCase();
  if (/amend|mod\s*\d|modification/.test(name)) return { label: "Amendment", badge: "doc-amendment" };
  if (/pricing|price\s*schedule|clin/.test(name)) return { label: "Pricing Schedule", badge: "doc-pricing" };
  if (/sf.?1449|sf1449/.test(name)) return { label: "SF-1449", badge: "doc-sf1449" };
  if (/wage|wd-|dav/.test(name)) return { label: "Wage Determination", badge: "doc-wage" };
  if (/pws|statement of work|sow|performance work/.test(name)) return { label: "PWS", badge: "doc-pws" };
  return { label: "Other", badge: "doc-other" };
}

function renderDocumentsTab(c) {
  const panel = document.getElementById("tab-panel-documents");
  const files = c.attachment_files?.files || [];
  const samAtts = c.sam_attachments || [];
  const amendmentBanner = c.amendment_alert_active
    ? `<div class="perf-banner perf-banner-red">AMENDMENT POSTED — review new documents before submitting.
        <button type="button" class="btn btn-secondary-action btn-sm" data-dismiss-amendments>Mark reviewed</button></div>`
    : "";

  const rows = files.length
    ? files.map((f) => {
        const cls = classifyDocument({ description: f.filename, filename: f.filename });
        const req =
          (c.pricing_schedule_required && cls.label === "Pricing Schedule")
            ? `<span class="doc-required doc-required-orange">REQUIRED</span>`
            : c.sf1449_required && cls.label === "SF-1449"
              ? `<span class="doc-required doc-required-blue">REQUIRED</span>`
              : "";
        return `<tr>
          <td><span class="doc-type-badge ${cls.badge}">${escapeHtml(cls.label)}</span> ${req}</td>
          <td>${escapeHtml(f.filename)}</td>
          <td>${f.file_size_bytes ? `${Math.round(f.file_size_bytes / 1024)} KB` : "—"}</td>
          <td><a class="btn btn-secondary-action btn-small" href="/api/contracts/${encodeURIComponent(c.notice_id)}/attachments/${f.id}/download" download>Download</a></td>
        </tr>`;
      })
    : samAtts.map((att, i) => {
        const cls = classifyDocument(att);
        const url = att.url || att.download_url || c.link;
        return `<tr>
          <td><span class="doc-type-badge ${cls.badge}">${escapeHtml(cls.label)}</span></td>
          <td>${escapeHtml(att.description || att.name || "Attachment")}</td>
          <td>—</td>
          <td>${url ? `<a class="btn btn-secondary-action btn-small" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open</a>` : "—"}</td>
        </tr>`;
      });

  panel.innerHTML = `
    ${amendmentBanner}
    <table class="pricing-table doc-table">
      <thead><tr><th>Type</th><th>File</th><th>Size</th><th></th></tr></thead>
      <tbody>${rows.join("") || '<tr><td colspan="4">No documents downloaded yet. Run sync to fetch attachments.</td></tr>'}</tbody>
    </table>
    <button type="button" class="btn btn-secondary-action" id="extract-solicitation-btn">Extract scope from PDFs</button>`;

  panel.querySelector("[data-dismiss-amendments]")?.addEventListener("click", async () => {
    await apiFetch(`/api/contracts/${encodeURIComponent(c.notice_id)}/amendments/dismiss`, { method: "POST" });
    const fresh = await fetchContract(c.notice_id);
    if (fresh) renderDocumentsTab(fresh);
  });
  document.getElementById("extract-solicitation-btn")?.addEventListener("click", () => {
    extractSolicitationMeta(c.notice_id).catch((err) => showSyncStatus(err.message, true));
  });
}

function renderSubsTab(noticeId) {
  const panel = document.getElementById("tab-panel-subs");
  panel.innerHTML = `<div id="contract-detail-subs-root"><p class="pricing-loading">Loading subs…</p></div>`;
  if (typeof loadContractSubsInto === "function") {
    loadContractSubsInto(noticeId, "contract-detail-subs-root");
  }
}

function renderProposalTab(c) {
  const panel = document.getElementById("tab-panel-proposal");
  panel.innerHTML = `<div id="contract-detail-proposal-root"><p class="pricing-loading">Loading proposal…</p></div>`;
  if (typeof loadProposalTabInto === "function") {
    loadProposalTabInto(c.notice_id, "contract-detail-proposal-root", c);
  }
}

function renderPerformanceTab(noticeId) {
  const panel = document.getElementById("tab-panel-performance");
  panel.innerHTML = `<div id="performance-tab-mount" class="performance-tab" data-notice-id="${escapeHtml(noticeId)}"></div>`;
  if (typeof loadContractPerformanceTab === "function") loadContractPerformanceTab(noticeId);
}

function stopContractDetailPolling() {
  if (contractDetailPollTimer) {
    clearInterval(contractDetailPollTimer);
    contractDetailPollTimer = null;
  }
}

function startContractDetailPolling(noticeId) {
  stopContractDetailPolling();
  contractDetailPollTimer = setInterval(async () => {
    if (activeContractId !== noticeId) {
      stopContractDetailPolling();
      return;
    }
    const c = await fetchContract(noticeId);
    if (!c) return;
    activeContractData = c;
    if (activeContractTab === "overview" && !document.getElementById("tab-panel-overview")) return;
    if (activeContractTab === "overview") renderOverviewTab(c);
  }, 5000);
}

async function beginContractDetailAnalysis(noticeId) {
  try {
    await requestContractScreening(noticeId);
    const c = await fetchContract(noticeId);
    if (c) {
      activeContractData = c;
      if (activeContractTab === "overview") renderOverviewTab(c);
      startContractDetailPolling(noticeId);
    }
  } catch (err) {
    if (err.message !== "Login required") showSyncStatus(err.message, true);
  }
}

function handleCardPrimaryAction(noticeId, action) {
  const map = {
    find_subs: () => openContractDetail(noticeId, "subs"),
    proposal: () => openContractDetail(noticeId, "proposal"),
    checklist: () => openContractDetail(noticeId, "proposal"),
    performance: () => openContractDetail(noticeId, "performance"),
    overview: () => openContractDetail(noticeId, "overview"),
    detail: () => openContractDetail(noticeId, "overview"),
  };
  (map[action] || map.overview)();
}

document.getElementById("contract-detail-back")?.addEventListener("click", () => {
  stopContractDetailPolling();
  activeContractId = null;
  showView("dashboard");
});
