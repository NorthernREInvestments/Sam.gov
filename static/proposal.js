/** Proposal Writer — 3-step pursue workflow */

let proposalNoticeId = null;
let proposalConfig = null;
let activeProposalId = null;

function startProposalWorkflow(noticeId) {
  proposalNoticeId = noticeId;
  proposalConfig = null;
  activeProposalId = null;
  showView("proposal-subs");
  loadProposalSubStep(noticeId);
}

async function loadProposalSubStep(noticeId) {
  const el = document.getElementById("proposal-step-content");
  if (!el) return;
  el.innerHTML = `<p class="pricing-loading">Loading subs…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/proposal/subs`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load subs");
    el.innerHTML = renderProposalSubStep(data);
    bindProposalSubStep(data);
  } catch (err) {
    el.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderProposalSubStep(data) {
  if (!data.has_quotes) {
    return `
      <div class="proposal-page">
        <button type="button" class="btn btn-secondary-action btn-small" id="proposal-back-dashboard">← Back</button>
        <h2>Select Subcontractor for ${escapeHtml(data.contract_title)}</h2>
        <div class="network-banner proposal-warning">
          You haven't recorded any sub quotes for this contract yet. Go to the Subs page and update at least one sub to <strong>Quote Received</strong> before writing a proposal.
        </div>
        <button type="button" class="btn btn-primary" id="proposal-go-subs">Go to Subs Page</button>
      </div>`;
  }
  return `
    <div class="proposal-page">
      <button type="button" class="btn btn-secondary-action btn-small" id="proposal-back-dashboard">← Back</button>
      <h2>Select Subcontractor for ${escapeHtml(data.contract_title)}</h2>
      <p class="detail-note">Choose the sub whose quote will be used in your bid.</p>
      <div class="subs-cards">${data.subs.map(renderProposalSubCard).join("")}</div>
    </div>`;
}

function renderProposalSubCard(sub) {
  return `
    <article class="sub-card proposal-sub-pick" data-contract-sub-id="${sub.contract_sub_id}">
      <h3 class="sub-card-title">${escapeHtml(sub.business_name)}</h3>
      <p class="sub-card-meta">${sub.rating != null ? `${sub.rating} ★ (${sub.review_count ?? 0})` : "No rating"} · ${sub.distance_miles != null ? `${sub.distance_miles} mi` : "—"}</p>
      <p class="sub-card-meta"><strong>Quote:</strong> ${formatMoney(sub.quote_amount)}</p>
      ${sub.contact_notes ? `<p class="sub-card-meta">${escapeHtml(sub.contact_notes)}</p>` : ""}
      <button type="button" class="btn btn-primary btn-small proposal-pick-sub">Use this sub</button>
    </article>`;
}

function bindProposalSubStep(data) {
  document.getElementById("proposal-back-dashboard")?.addEventListener("click", () => showView("dashboard"));
  document.getElementById("proposal-go-subs")?.addEventListener("click", () => {
    if (proposalNoticeId) openContractSubs(proposalNoticeId);
  });
  document.querySelectorAll(".proposal-pick-sub").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const card = e.target.closest(".proposal-sub-pick");
      const id = Number(card?.dataset.contractSubId);
      if (id) loadProposalConfigStep(proposalNoticeId, id);
    });
  });
}

async function loadProposalConfigStep(noticeId, contractSubId) {
  showView("proposal-config");
  const el = document.getElementById("proposal-config-content");
  el.innerHTML = `<p class="pricing-loading">Building bid configuration…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/proposal/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_sub_id: contractSubId }),
    });
    const config = await res.json();
    if (!res.ok) throw new Error(config.detail || "Config failed");
    proposalConfig = config;
    el.innerHTML = renderProposalConfigStep(config);
    bindProposalConfigStep(noticeId);
  } catch (err) {
    el.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function cfgInputClass(missing, fieldKey, extra = "") {
  const miss = (missing || []).some((m) => m.field === fieldKey);
  return `settings-input cfg-field${extra ? ` ${extra}` : ""}${miss ? " field-missing" : ""}`;
}

function renderProposalConfigStep(config) {
  const a = config.section_a || {};
  const b = config.section_b || {};
  const c = config.section_c || {};
  const d = config.section_d || {};
  const sub = config.sub || {};
  const rs = c.bid_range_status || {};
  const opt = c.option_years || {};
  const missing = config.missing_fields || [];
  const missingBanner = missing.length
    ? `<div class="network-banner proposal-warning">Action Required: ${missing.length} field(s) need attention in Settings or solicitation data before submitting.</div>`
    : "";
  const confirm = `<div class="network-banner proposal-confirm">Using <strong>${escapeHtml(sub.business_name)}</strong> — Quote: ${formatMoney(sub.quote_amount)}. This quote will be used to calculate your bid.</div>`;

  return `
    <div class="proposal-page">
      <button type="button" class="btn btn-secondary-action btn-small" id="proposal-back-subs">← Back to sub selection</button>
      <h2>Bid configuration</h2>
      ${confirm}
      ${missingBanner}
      <section class="settings-section">
        <h3>Section A — Contract information</h3>
        <label class="filter-label">Contract title</label><input class="settings-input cfg-a" data-key="contract_title" value="${escapeHtml(a.contract_title || "")}" readonly>
        <label class="filter-label">Solicitation number</label><input class="settings-input cfg-a" data-key="solicitation_number" value="${escapeHtml(a.solicitation_number || "")}">
        <label class="filter-label">Agency</label><input class="settings-input cfg-a" data-key="agency_name" value="${escapeHtml(a.agency_name || "")}" readonly>
        <label class="filter-label">Contracting Officer</label><input class="${cfgInputClass(missing, "contracting_officer_name")} cfg-a" data-key="contracting_officer_name" value="${escapeHtml(a.contracting_officer_name || "")}">
        <label class="filter-label">CO email</label><input class="${cfgInputClass(missing, "contracting_officer_email")} cfg-a" data-key="contracting_officer_email" value="${escapeHtml(a.contracting_officer_email || "")}">
        <label class="filter-label">Submission method</label><input class="${cfgInputClass(missing, "submission_method")} cfg-a" data-key="submission_method" value="${escapeHtml(a.submission_method || "")}">
        <label class="filter-label">Deadline</label><input class="settings-input cfg-a" data-key="submission_deadline" value="${escapeHtml(a.submission_deadline || "")}">
        <label class="filter-label">Place of performance</label><input class="settings-input cfg-a" data-key="place_of_performance" value="${escapeHtml(a.place_of_performance || "")}" readonly>
      </section>
      <section class="settings-section">
        <h3>Section B — Your business</h3>
        <label class="filter-label">Legal name</label><input class="settings-input cfg-b" data-key="legal_business_name" value="${escapeHtml(b.legal_business_name || "")}">
        <label class="filter-label">Owner</label><input class="settings-input cfg-b" data-key="owner_name" value="${escapeHtml(b.owner_name || "")}">
        <label class="filter-label">Address</label><input class="${cfgInputClass(missing, "address_line_1")} cfg-b" data-key="address_line_1" value="${escapeHtml(b.address_line_1 || "")}">
        <label class="filter-label">City / State / ZIP</label>
        <input class="${cfgInputClass(missing, "city")} cfg-b" data-key="city" placeholder="City" value="${escapeHtml(b.city || "")}">
        <input class="settings-input cfg-b subs-state-input" data-key="state" placeholder="ST" value="${escapeHtml(b.state || "")}">
        <input class="${cfgInputClass(missing, "zip")} cfg-b" data-key="zip" placeholder="ZIP" value="${escapeHtml(b.zip || "")}">
        <label class="filter-label">UEI / CAGE / EIN</label>
        <input class="${cfgInputClass(missing, "uei")} cfg-b" data-key="uei" placeholder="UEI" value="${escapeHtml(b.uei || "")}">
        <input class="${cfgInputClass(missing, "cage_code")} cfg-b" data-key="cage_code" placeholder="CAGE" value="${escapeHtml(b.cage_code || "")}">
        <input class="${cfgInputClass(missing, "ein")} cfg-b" data-key="ein" placeholder="EIN" value="${escapeHtml(b.ein || "")}">
        <label class="filter-label">Phone / Email</label>
        <input class="settings-input cfg-b" data-key="business_phone" value="${escapeHtml(b.business_phone || "")}">
        <input class="settings-input cfg-b" data-key="business_email" value="${escapeHtml(b.business_email || "")}">
      </section>
      <section class="settings-section">
        <h3>Section C — Pricing</h3>
        <p class="detail-note">Sub quote: <strong>${formatMoney(c.sub_quote)}</strong> (fixed)</p>
        <label class="filter-label">Your margin <span id="prop-margin-label">${c.margin_percentage}%</span></label>
        <input type="range" id="prop-margin" min="10" max="35" step="1" value="${c.margin_percentage || 20}">
        <div class="pricing-stats" id="prop-pricing-stats">
          <div class="pricing-stat pricing-stat-highlight"><span class="pricing-stat-label">Base year bid</span><span class="pricing-stat-value" id="prop-base-bid">${formatMoney(c.base_year_bid)}</span></div>
          <div class="pricing-stat"><span class="pricing-stat-label">Your profit</span><span class="pricing-stat-value" id="prop-profit">${formatMoney(c.base_year_profit)}</span></div>
          <div class="pricing-stat"><span class="pricing-stat-label">Total all years</span><span class="pricing-stat-value" id="prop-total">${formatMoney(c.total_all_years)}</span></div>
        </div>
        <p class="pricing-note calc-range-${rs.level || "neutral"}" id="prop-range-status">${escapeHtml(rs.message || "")}</p>
        <table class="pricing-table"><thead><tr><th>Period</th><th>Amount</th></tr></thead><tbody>
          <tr><td>Base Year</td><td id="prop-oy-base">${formatMoney(opt.base_year || c.base_year_bid)}</td></tr>
          <tr><td>Option Year 1</td><td id="prop-oy-1">${formatMoney(opt.option_year_1)}</td></tr>
          <tr><td>Option Year 2</td><td id="prop-oy-2">${formatMoney(opt.option_year_2)}</td></tr>
          <tr><td>Option Year 3</td><td id="prop-oy-3">${formatMoney(opt.option_year_3)}</td></tr>
          <tr><td>Option Year 4</td><td id="prop-oy-4">${formatMoney(opt.option_year_4)}</td></tr>
        </tbody></table>
        <label class="filter-label">Option year increase %</label>
        <input type="number" id="prop-oy-pct" class="settings-input" value="${c.option_year_increase_pct || 3}" step="0.5" min="0" max="15">
      </section>
      <section class="settings-section">
        <h3>Section D — Options</h3>
        <label class="checkbox-inline"><input type="checkbox" id="prop-past-perf" ${d.include_past_performance !== false ? "checked" : ""}> Include past performance</label>
        <label class="checkbox-inline"><input type="checkbox" id="prop-capability" ${d.include_capability_statement !== false ? "checked" : ""}> Include capability statement</label>
        <label class="filter-label">Writing tone</label>
        <select id="prop-tone" class="settings-input"><option>Professional</option><option>Confident</option><option>Conservative</option></select>
        <label class="filter-label">Technical detail</label>
        <select id="prop-detail" class="settings-input"><option>Detailed</option><option>Standard</option></select>
      </section>
      <button type="button" class="btn btn-pursue-active pursue-btn" id="proposal-generate-btn">Generate Proposal</button>
    </div>`;
}

function bindProposalConfigStep(noticeId) {
  document.getElementById("proposal-back-subs")?.addEventListener("click", () => loadProposalSubStep(noticeId));
  const recalc = () => updateProposalPricingFromUI();
  document.getElementById("prop-margin")?.addEventListener("input", recalc);
  document.getElementById("prop-oy-pct")?.addEventListener("input", recalc);
  document.getElementById("proposal-generate-btn")?.addEventListener("click", () => generateProposal(noticeId));
}

function updateProposalPricingFromUI() {
  if (!proposalConfig?.section_c) return;
  const sub = proposalConfig.section_c.sub_quote;
  const margin = Number(document.getElementById("prop-margin")?.value || 20);
  const increase = Number(document.getElementById("prop-oy-pct")?.value || 3);
  document.getElementById("prop-margin-label").textContent = `${margin}%`;
  const base = sub / (1 - margin / 100);
  const profit = base - sub;
  let prev = base;
  const mult = 1 + increase / 100;
  const years = { base_year: base, option_year_1: prev * mult };
  prev = years.option_year_1;
  years.option_year_2 = prev * mult;
  prev = years.option_year_2;
  years.option_year_3 = prev * mult;
  prev = years.option_year_3;
  years.option_year_4 = prev * mult;
  const total = base + years.option_year_1 + years.option_year_2 + years.option_year_3 + years.option_year_4;
  proposalConfig.section_c.margin_percentage = margin;
  proposalConfig.section_c.base_year_bid = Math.round(base * 100) / 100;
  proposalConfig.section_c.base_year_profit = Math.round(profit * 100) / 100;
  proposalConfig.section_c.option_years = Object.fromEntries(
    Object.entries(years).map(([k, v]) => [k, Math.round(v * 100) / 100])
  );
  proposalConfig.section_c.total_all_years = Math.round(total * 100) / 100;
  proposalConfig.section_c.option_year_increase_pct = increase;
  document.getElementById("prop-base-bid").textContent = formatMoney(base);
  document.getElementById("prop-profit").textContent = formatMoney(profit);
  document.getElementById("prop-total").textContent = formatMoney(total);
  document.getElementById("prop-oy-base").textContent = formatMoney(base);
  document.getElementById("prop-oy-1").textContent = formatMoney(years.option_year_1);
  document.getElementById("prop-oy-2").textContent = formatMoney(years.option_year_2);
  document.getElementById("prop-oy-3").textContent = formatMoney(years.option_year_3);
  document.getElementById("prop-oy-4").textContent = formatMoney(years.option_year_4);
}

function collectConfigFromUI() {
  if (!proposalConfig) return null;
  document.querySelectorAll(".cfg-a").forEach((el) => {
    proposalConfig.section_a[el.dataset.key] = el.value;
  });
  document.querySelectorAll(".cfg-b").forEach((el) => {
    proposalConfig.section_b[el.dataset.key] = el.value;
  });
  proposalConfig.section_d = {
    include_past_performance: document.getElementById("prop-past-perf")?.checked,
    include_capability_statement: document.getElementById("prop-capability")?.checked,
    writing_tone: document.getElementById("prop-tone")?.value,
    technical_detail: document.getElementById("prop-detail")?.value,
  };
  updateProposalPricingFromUI();
  return proposalConfig;
}

async function generateProposal(noticeId) {
  const config = collectConfigFromUI();
  const btn = document.getElementById("proposal-generate-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Generating…"; }
  showSyncStatus("Claude is writing your proposal — this may take 1–2 minutes.");
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/proposal/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Generation failed");
    activeProposalId = data.id;
    showView("proposal-editor");
    renderProposalEditor(data);
    showSyncStatus("Proposal generated.");
  } catch (err) {
    showSyncStatus(err.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Generate Proposal"; }
  }
}

function renderProposalEditor(data) {
  const el = document.getElementById("proposal-editor-content");
  if (!el) return;
  const sections = data.sections || {};
  const titles = data.section_titles || {};
  const keys = Object.keys(titles).length ? Object.keys(titles) : Object.keys(sections);
  const missing = data.missing_fields || [];
  const versions = data.versions || [];
  const versionList = versions.length
    ? `<div class="proposal-versions">
        <h4>Version history</h4>
        <ul class="proposal-version-list">${versions.slice().reverse().map((v) => `
          <li>
            <button type="button" class="proposal-version-btn" data-version="${v.index}" title="${escapeHtml(v.preview || "")}">
              ${v.saved_at ? new Date(v.saved_at).toLocaleString() : "Saved version"}
              ${v.note ? ` · ${escapeHtml(v.note)}` : ""}
            </button>
          </li>`).join("")}</ul>
        <p class="detail-note">Every save creates a version. Click one to roll back.</p>
      </div>`
    : `<p class="detail-note">No saved versions yet — Save Draft creates your first rollback point.</p>`;
  el.innerHTML = `
    <div class="proposal-editor-layout">
      <aside class="proposal-sidebar">
        <button type="button" class="btn btn-secondary-action btn-small" id="proposal-editor-back">← Back</button>
        <h3>Sections</h3>
        <nav class="proposal-nav">${keys.map((k) => `<button type="button" class="proposal-nav-btn" data-section="${k}">${escapeHtml(titles[k] || k)} <span class="proposal-wc">${data.word_counts?.[k] || 0}w</span></button>`).join("")}</nav>
        <p class="detail-note">Total: ${data.total_word_count || 0} words</p>
        ${versionList}
        <button type="button" class="btn btn-secondary-action btn-small" id="proposal-humanize" disabled title="Select text in the proposal first">Make More Human</button>
        <button type="button" class="btn btn-secondary-action btn-small" id="proposal-reduce-ai" title="Second pass on the full proposal to sound less AI-generated">Reduce AI Score</button>
        <button type="button" class="btn btn-primary btn-small" id="proposal-save-draft">Save Draft</button>
        <button type="button" class="btn btn-pursue-active btn-small" id="proposal-mark-submitted" ${missing.length ? "disabled title='Fill all required fields first'" : ""}>Mark as Submitted</button>
        <p class="detail-note">Mark submitted after you file on SAM.gov — prevents duplicate bids on this contract.</p>
        <div class="proposal-downloads">
          <h4>Download</h4>
          <button type="button" class="btn btn-secondary-action btn-small" id="proposal-download-word">Download as Word</button>
          <button type="button" class="btn btn-secondary-action btn-small" id="proposal-download-pdf">Download as PDF</button>
          <button type="button" class="btn btn-secondary-action btn-small" id="proposal-download-capability">Capability Statement PDF</button>
        </div>
        <label class="filter-label">Internal notes</label>
        <textarea id="proposal-notes" class="settings-input" rows="3">${escapeHtml(data.notes || "")}</textarea>
      </aside>
      <main class="proposal-main">
        ${missing.length ? `<div class="network-banner proposal-warning">Action Required: ${missing.length} fields need attention. Check Settings.</div>` : ""}
        <div id="proposal-sections">${keys.map((k) => `
          <section class="proposal-section-block" id="section-${k}">
            <div class="proposal-section-header">
              <h3>${escapeHtml(titles[k] || k)}</h3>
              <button type="button" class="btn btn-secondary-action btn-small regen-section" data-section="${k}">Regenerate section</button>
            </div>
            <div class="proposal-editable" contenteditable="true" data-section="${k}">${sections[k] || ""}</div>
          </section>`).join("")}</div>
      </main>
    </div>`;
  bindProposalEditor(data);
}

let proposalSelectionListener = null;

function getEditorSelectionHtml() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const anchor = sel.anchorNode;
  const editable = anchor?.nodeType === Node.TEXT_NODE ? anchor.parentElement?.closest(".proposal-editable") : anchor?.closest?.(".proposal-editable");
  if (!editable) return null;
  const range = sel.getRangeAt(0);
  const wrap = document.createElement("div");
  wrap.appendChild(range.cloneContents());
  const text = wrap.textContent?.trim();
  if (!text || text.length < 3) return null;
  return wrap.innerHTML;
}

function replaceEditorSelection(html) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return false;
  const range = sel.getRangeAt(0);
  range.deleteContents();
  const temp = document.createElement("div");
  temp.innerHTML = html;
  const frag = document.createDocumentFragment();
  while (temp.firstChild) frag.appendChild(temp.firstChild);
  range.insertNode(frag);
  sel.removeAllRanges();
  return true;
}

function updateHumanizeButtonState() {
  const btn = document.getElementById("proposal-humanize");
  if (!btn) return;
  btn.disabled = !getEditorSelectionHtml();
}

function bindProposalEditor(data) {
  document.getElementById("proposal-editor-back")?.addEventListener("click", () => showView("dashboard"));
  document.querySelectorAll(".proposal-nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(`section-${btn.dataset.section}`)?.scrollIntoView({ behavior: "smooth" });
    });
  });
  document.getElementById("proposal-save-draft")?.addEventListener("click", () => saveProposalDraft());
  document.getElementById("proposal-mark-submitted")?.addEventListener("click", () => markProposalSubmitted());
  document.getElementById("proposal-reduce-ai")?.addEventListener("click", () => reduceProposalAi());
  document.getElementById("proposal-humanize")?.addEventListener("click", () => humanizeSelectedText());
  document.getElementById("proposal-download-word")?.addEventListener("click", () => downloadProposalExport("docx"));
  document.getElementById("proposal-download-pdf")?.addEventListener("click", () => downloadProposalExport("pdf"));
  document.getElementById("proposal-download-capability")?.addEventListener("click", () => downloadProposalExport("capability-pdf"));
  document.querySelectorAll(".regen-section").forEach((btn) => {
    btn.addEventListener("click", () => regenerateProposalSection(btn.dataset.section));
  });
  document.querySelectorAll(".proposal-version-btn").forEach((btn) => {
    btn.addEventListener("click", () => restoreProposalVersion(Number(btn.dataset.version)));
  });
  if (proposalSelectionListener) {
    document.removeEventListener("selectionchange", proposalSelectionListener);
  }
  proposalSelectionListener = () => updateHumanizeButtonState();
  document.addEventListener("selectionchange", proposalSelectionListener);
  updateHumanizeButtonState();
}

async function restoreProposalVersion(versionIndex) {
  if (!activeProposalId) return;
  if (!confirm("Restore this version? Your current text will be saved to history first.")) return;
  showSyncStatus("Restoring version…");
  const res = await apiFetch(`/api/proposals/${activeProposalId}/restore-version`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version_index: versionIndex }),
  });
  const data = await res.json();
  if (!res.ok) {
    showSyncStatus(data.detail || "Restore failed", true);
    return;
  }
  renderProposalEditor(data);
  showSyncStatus("Version restored.");
}

async function openProposalEditor(proposalId, noticeId) {
  activeProposalId = proposalId;
  if (noticeId) proposalNoticeId = noticeId;
  showView("proposal-editor");
  const el = document.getElementById("proposal-editor-content");
  if (el) el.innerHTML = `<p class="pricing-loading">Loading proposal…</p>`;
  try {
    const res = await apiFetch(`/api/proposals/${proposalId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load proposal");
    renderProposalEditor(data);
  } catch (err) {
    if (el) el.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

async function saveProposalDraft() {
  if (!activeProposalId) return;
  const sections = {};
  document.querySelectorAll(".proposal-editable").forEach((el) => {
    sections[el.dataset.section] = el.innerHTML;
  });
  const html = Object.values(sections).join("\n\n");
  const res = await apiFetch(`/api/proposals/${activeProposalId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      proposal_html: html,
      sections_json: sections,
      notes: document.getElementById("proposal-notes")?.value,
      status: "draft",
    }),
  });
  const data = await res.json();
  if (res.ok) {
    renderProposalEditor(data);
    showSyncStatus("Draft saved — version added to history.");
  } else {
    showSyncStatus(data.detail || "Save failed", true);
  }
}

async function markProposalSubmitted() {
  if (!activeProposalId) return;
  const sections = {};
  document.querySelectorAll(".proposal-editable").forEach((el) => {
    sections[el.dataset.section] = el.innerHTML;
  });
  const html = Object.values(sections).join("\n\n");
  const res = await apiFetch(`/api/proposals/${activeProposalId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      proposal_html: html,
      sections_json: sections,
      notes: document.getElementById("proposal-notes")?.value,
      status: "submitted",
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    showSyncStatus(data.detail || "Could not mark submitted", true);
    return;
  }
  showSyncStatus("Marked as submitted — this contract is locked against duplicate bids.");
  showView("dashboard");
  await loadContracts();
}

async function humanizeSelectedText() {
  if (!activeProposalId) return;
  const selected = getEditorSelectionHtml();
  if (!selected) {
    showSyncStatus("Select some proposal text first.", true);
    return;
  }
  const btn = document.getElementById("proposal-humanize");
  if (btn) btn.disabled = true;
  showSyncStatus("Rewriting selection…");
  try {
    const res = await apiFetch(`/api/proposals/${activeProposalId}/humanize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: selected }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Humanize failed");
    if (!replaceEditorSelection(data.html || selected)) {
      showSyncStatus("Could not replace selection — try again.", true);
      return;
    }
    showSyncStatus("Selection updated. Save draft to keep this version.");
  } catch (err) {
    showSyncStatus(err.message, true);
  } finally {
    updateHumanizeButtonState();
  }
}

async function regenerateProposalSection(sectionKey) {
  if (!activeProposalId) return;
  showSyncStatus(`Regenerating ${sectionKey}…`);
  const res = await apiFetch(`/api/proposals/${activeProposalId}/regenerate-section`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section_key: sectionKey }),
  });
  const data = await res.json();
  if (!res.ok) { showSyncStatus(data.detail || "Failed", true); return; }
  renderProposalEditor(data);
  showSyncStatus("Section regenerated.");
}

async function downloadProposalExport(kind) {
  if (!activeProposalId) return;
  const sections = {};
  document.querySelectorAll(".proposal-editable").forEach((el) => {
    sections[el.dataset.section] = el.innerHTML;
  });
  const labels = { docx: "Word document", pdf: "PDF", "capability-pdf": "Capability statement" };
  showSyncStatus(`Building ${labels[kind] || "file"}…`);
  try {
    const res = await apiFetch(`/api/proposals/${activeProposalId}/export/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sections_json: sections }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Download failed");
    }
    const blob = await res.blob();
    const disp = res.headers.get("Content-Disposition") || "";
    const match = disp.match(/filename=\"?([^\";]+)\"?/i);
    const filename = match ? match[1] : `proposal.${kind === "docx" ? "docx" : "pdf"}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showSyncStatus(`Downloaded ${filename}`);
  } catch (err) {
    showSyncStatus(err.message, true);
  }
}

async function reduceProposalAi() {
  if (!activeProposalId) return;
  showSyncStatus("Running AI-score reduction pass…");
  const res = await apiFetch(`/api/proposals/${activeProposalId}/reduce-ai-score`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { showSyncStatus(data.detail || "Failed", true); return; }
  renderProposalEditor(data);
  showSyncStatus("Proposal updated.");
}

function renderPursueButton(c) {
  if (c.pursue !== true) return "";
  if (c.pipeline?.do_not_rebid || c.workflow?.do_not_rebid) {
    return `<p class="detail-note card-rebid-block">Bid already submitted — open Continue proposal to review.</p>`;
  }
  return `<button type="button" class="btn btn-pursue-active btn-small card-pursue-btn" data-pursue="${escapeHtml(c.notice_id)}">Pursue</button>`;
}

function renderWorkflowBanner(c) {
  const wf = c.workflow || {};
  if (!wf.incomplete || wf.stage === "none") return "";
  const items = (wf.items || [])
    .slice(0, 3)
    .map((i) => i.label)
    .filter(Boolean)
    .join(" · ");
  return `<div class="card-workflow-banner card-workflow-banner-${wf.stage}">
    <strong>${escapeHtml(wf.label || "In progress")}</strong>${items ? `<span class="card-workflow-items"> — ${escapeHtml(items)}</span>` : ""}
  </div>`;
}

function renderContinueProposal(c) {
  const wf = c.workflow || {};
  if (!wf.proposal_id) return "";
  return `<button type="button" class="btn btn-secondary-action btn-small card-continue-proposal" data-continue-proposal="${wf.proposal_id}" data-notice-id="${escapeHtml(c.notice_id)}">Continue proposal</button>`;
}

function bindProposalPursueClicks() {
  document.body.addEventListener("click", (e) => {
    const continueBtn = e.target.closest("[data-continue-proposal]");
    if (continueBtn) {
      e.preventDefault();
      e.stopPropagation();
      openProposalEditor(Number(continueBtn.dataset.continueProposal), continueBtn.dataset.noticeId);
      return;
    }
    const btn = e.target.closest("[data-pursue]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    startProposalWorkflow(btn.dataset.pursue);
  });
}

document.addEventListener("DOMContentLoaded", bindProposalPursueClicks);
