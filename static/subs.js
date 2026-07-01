/** Sub Finder UI — contract subs, master database, outreach tracking. */

const SUB_CONTACT_STATUSES = [
  "Not Contacted",
  "Contacted",
  "Voicemail Left",
  "Quote Received",
  "Selected",
  "Not Selected",
];

const STATUS_BADGE_CLASS = {
  "Not Contacted": "sub-status-grey",
  Contacted: "sub-status-blue",
  "Voicemail Left": "sub-status-yellow",
  "Quote Received": "sub-status-green",
  Selected: "sub-status-selected",
  "Not Selected": "sub-status-red",
};

let activeContractSubsId = null;
let contractSubsPollTimer = null;
let mySubsCache = [];
let contractSubsData = null;
let subsActiveTab = "list";

function subSummaryLine(c) {
  const s = c.sub_summary || {};
  const count = s.recommended_count ?? s.count ?? 0;
  const radius = s.radius_miles || 25;
  const city = s.city || c.location || "this area";
  if (s.status === "searching") return "Searching for subs near you…";
  if (!count) return null;
  return `${count} recommended subs found within ${radius} miles of ${city}`;
}

function renderSubSummaryLink(c) {
  const line = subSummaryLine(c);
  const s = c.sub_summary || {};
  if (s.status === "searching") {
    return `<p class="detail-note">Finding subcontractors via Google Places…</p>`;
  }
  if (!line && !(s.count > 0)) {
    return `<p class="detail-note">No subs found yet. <button type="button" class="link-button" data-find-subs="${escapeHtml(c.notice_id)}">Find subs</button></p>`;
  }
  const text = line || `${s.count} sub(s) linked to this contract`;
  return `<p class="detail-note"><button type="button" class="link-button subs-summary-link" data-open-subs="${escapeHtml(c.notice_id)}">${escapeHtml(text)}</button></p>`;
}

function renderPursueSection(c) {
  const pursue = c.pursue === true;
  const label = pursue ? "Pursue this contract" : c.pursue === false ? "Skip this contract" : "Screening pending";
  const btnClass = pursue ? "btn-pursue-active" : "btn-secondary-action";
  const disabled = pursue ? "" : "disabled";
  const pursueAttr = pursue ? ` data-pursue="${escapeHtml(c.notice_id)}"` : "";
  return `
    <div class="pursue-section">
      <div class="modal-badges">${screeningBadge(c)}</div>
      <p class="detail-item-value">${escapeHtml(c.reason || c.analysis?.reason || "Run screening to get a pursue/skip recommendation.")}</p>
      <button type="button" class="btn ${btnClass} pursue-btn"${pursueAttr} ${disabled}>${escapeHtml(label)}</button>
      ${c.selected_sub_quote ? `<p class="detail-note">Selected sub quote: ${formatMoney(c.selected_sub_quote)}</p>` : ""}
    </div>`;
}

function renderNetworkBanner(c) {
  const count = c.nearby_network_count || 0;
  if (!count) return "";
  return `
    <div class="network-banner">
      You have ${count} sub${count === 1 ? "" : "s"} already in your network near this location.
      <button type="button" class="btn btn-secondary-action btn-small" data-add-network="${escapeHtml(c.notice_id)}">Add to this contract</button>
    </div>`;
}

async function findSubs(noticeId, { force = false } = {}) {
  showSyncStatus("Searching for subcontractors…");
  const qs = force ? "?force=true" : "";
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/find-subs${qs}`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Sub search failed");
  markContractSubSearchPending(noticeId);
  showSyncStatus("Sub search started — results will appear shortly.");
  await loadContracts();
  if (activeDetailId === noticeId) {
    startContractSubsPolling(noticeId);
  }
}

async function addNetworkSubs(noticeId) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/nearby-subs`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not load network subs");
  if (!data.subs?.length) {
    showSyncStatus("No network subs found near this contract.", true);
    return;
  }
  const ids = data.subs.slice(0, 10).map((s) => s.id);
  const addRes = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/subs/add-network`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sub_ids: ids }),
  });
  const addData = await addRes.json();
  if (!addRes.ok) throw new Error(addData.detail || "Could not add subs");
  showSyncStatus(`Added ${addData.added_from_network || ids.length} subs from your network.`);
  await loadContracts();
}

function renderFindSubsButton(c) {
  return `<button type="button" class="btn btn-secondary-action btn-small card-find-subs" data-find-subs="${escapeHtml(c.notice_id)}">Find Subs</button>`;
}

function renderCardSubSummary(c) {
  const s = c.sub_summary || {};
  const searching = s.status === "searching" || c.sub_search_status === "searching";
  if (searching) {
    return `
      <div class="card-section card-section-subs">
        <span class="card-label">Subs</span>
        <p class="card-meta card-subs-searching">Finding subcontractors near this site…</p>
      </div>`;
  }
  const line = subSummaryLine(c);
  if (line) {
    return `
      <div class="card-section card-section-subs">
        <span class="card-label">Subs</span>
        <p class="card-meta">
          <button type="button" class="link-button subs-summary-link" data-open-subs="${escapeHtml(c.notice_id)}">${escapeHtml(line)}</button>
        </p>
      </div>`;
  }
  if (s.status === "complete" && !(s.count > 0)) {
    return `
      <div class="card-section card-section-subs">
        <span class="card-label">Subs</span>
        <p class="card-meta">No subs found automatically.</p>
      </div>`;
  }
  return "";
}

function markContractSubSearchPending(noticeId) {
  const row = contracts.find((c) => c.notice_id === noticeId);
  if (!row) return;
  row.sub_search_status = "searching";
  row.sub_summary = { ...(row.sub_summary || {}), status: "searching" };
  renderCards();
  manageCardPolling();
}

function openContractSubs(noticeId) {
  if (typeof openContractDetail === "function") {
    openContractDetail(noticeId, "subs");
    return;
  }
  activeContractSubsId = noticeId;
  subsActiveTab = "list";
  loadContractSubsInto(noticeId, "contract-subs-content");
}

async function loadContractSubsInto(noticeId, containerId, { quiet = false } = {}) {
  const container = document.getElementById(containerId);
  if (!container) return;
  activeContractSubsId = noticeId;
  if (!quiet) container.innerHTML = `<p class="pricing-loading">Loading subs…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/subs`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load subs");
    contractSubsData = data;
    if (data.summary?.status === "searching") startContractSubsPolling(noticeId, containerId);
    else stopContractSubsPolling();
    container.innerHTML = renderContractSubsPage(data);
    bindContractSubsPage(container, noticeId);
  } catch (err) {
    if (!quiet) container.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

async function loadContractSubsPage(noticeId, { quiet = false } = {}) {
  return loadContractSubsInto(noticeId, "contract-subs-content", { quiet });
}

function stopContractSubsPolling() {
  if (contractSubsPollTimer) {
    clearInterval(contractSubsPollTimer);
    contractSubsPollTimer = null;
  }
}

function startContractSubsPolling(noticeId, containerId = "contract-subs-content") {
  stopContractSubsPolling();
  contractSubsPollTimer = setInterval(() => {
    if (activeContractSubsId !== noticeId) {
      stopContractSubsPolling();
      return;
    }
    loadContractSubsInto(noticeId, containerId, { quiet: true });
  }, 4000);
}

function copyTextToClipboard(text, message) {
  navigator.clipboard.writeText(text).then(() => {
    showSyncStatus(message || "Copied to clipboard.");
  }).catch(() => showSyncStatus("Could not copy — select and copy manually.", true));
}

function renderWageBanner(wage) {
  if (!wage) return "";
  const rate = wage.hourly_rate != null ? `$${Number(wage.hourly_rate).toFixed(2)}` : "—";
  const minMo = wage.minimum_monthly_quote != null ? formatMoney(wage.minimum_monthly_quote) : "—";
  return `
    <div class="wage-requirement-banner">
      <h3>Wage requirement</h3>
      <div class="wage-banner-grid">
        <div><span class="card-label">WD number</span><p>${escapeHtml(wage.wage_determination_number || "Not extracted")}</p></div>
        <div><span class="card-label">Min hourly rate</span><p>${rate}/hr</p></div>
        <div><span class="card-label">Est. employees</span><p>${wage.estimated_employees ?? "—"}</p></div>
        <div><span class="card-label">Min monthly quote</span><p>${minMo}</p></div>
      </div>
      <p class="wage-banner-warning">${escapeHtml(wage.warning_text || "")}</p>
    </div>`;
}

function renderProgressTracker(progress) {
  const p = progress || {};
  const pct = p.quote_progress_pct ?? 0;
  return `
    <div class="sub-progress-tracker">
      <div class="sub-progress-stats">
        <span><strong>${p.total ?? 0}</strong> subs found</span>
        <span><strong>${p.called ?? 0}</strong> called</span>
        <span><strong>${p.reached ?? 0}</strong> reached</span>
        <span><strong>${p.quoted ?? 0}</strong> quoted</span>
        <span class="sub-progress-target">Target: ${p.quote_target ?? 3} quotes before bidding</span>
      </div>
      <div class="sub-progress-bar-wrap" aria-label="Quote progress">
        <div class="sub-progress-bar" style="width:${Math.min(100, pct)}%"></div>
      </div>
      <p class="sub-progress-label">${p.quoted ?? 0} of ${p.quote_target ?? 3} quotes received</p>
    </div>`;
}

function renderPreBidChecklist(checklist, noticeId) {
  const c = checklist || {};
  const items = c.items || [];
  return `
    <div class="pre-bid-checklist ${c.all_complete ? "checklist-complete" : ""}">
      <h3>Pre-bid requirements</h3>
      <ul class="pre-bid-items">
        ${items.map((item) => `
          <li class="${item.complete ? "check-ok" : "check-missing"}">
            <span>${item.complete ? "✓" : "○"}</span>
            <span>${escapeHtml(item.label)}</span>
            <span class="pre-bid-detail">${escapeHtml(item.detail || "")}</span>
          </li>`).join("")}
      </ul>
      ${!c.can_proceed ? `<p class="pre-bid-block-msg">${escapeHtml(c.block_message || "")}</p>` : ""}
      ${!c.all_complete && !c.bypassed ? `
        <button type="button" class="btn btn-secondary-action btn-small" data-bypass-checklist="${escapeHtml(noticeId)}">
          Override checklist and proceed anyway
        </button>` : ""}
      ${c.bypassed ? `<p class="detail-note">Checklist bypassed ${c.bypassed_at ? new Date(c.bypassed_at).toLocaleString() : ""}</p>` : ""}
    </div>`;
}

function renderStatusBadge(status) {
  const cls = STATUS_BADGE_CLASS[status] || "sub-status-grey";
  return `<span class="sub-status-badge ${cls}">${escapeHtml(status || "Not Contacted")}</span>`;
}

function renderContactCard(contact) {
  const followup = contact.needs_followup
    ? `<span class="followup-badge">Follow up needed — no quote received in 48 hours</span>`
    : "";
  const stars = contact.rating != null ? `${contact.rating} ★` : "No rating";
  const dist = contact.distance_miles != null ? `${contact.distance_miles} mi` : "—";
  const loc = [contact.city, contact.state].filter(Boolean).join(", ") || "—";
  return `
    <article class="sub-contact-card ${contact.is_selected ? "sub-contact-selected" : ""}" data-contact-id="${contact.id}">
      ${followup}
      <header class="sub-contact-header">
        <div>
          <h3>${escapeHtml(contact.company_name)}</h3>
          <p class="sub-card-meta">${escapeHtml(stars)} · ${escapeHtml(loc)} · ${dist}</p>
        </div>
        ${renderStatusBadge(contact.status)}
      </header>
      <div class="sub-contact-actions-row">
        <button type="button" class="btn btn-secondary-action btn-small sub-copy-phone" data-phone="${escapeHtml(contact.phone || "")}">
          ${escapeHtml(contact.phone || "No phone")}
        </button>
        <button type="button" class="btn btn-secondary-action btn-small" data-call-contact="${contact.id}">Call</button>
        <button type="button" class="btn btn-secondary-action btn-small" data-log-quote="${contact.id}">Log Quote</button>
        <button type="button" class="btn btn-secondary-action btn-small" data-voicemail="${contact.id}">Voicemail Script</button>
        <button type="button" class="btn btn-primary-action btn-small" data-view-contact="${contact.id}">View Details</button>
      </div>
    </article>`;
}

function renderQuoteComparisonTable(rows) {
  if (!rows?.length) return `<p class="empty">No quotes received yet.</p>`;
  return `
    <table class="quote-comparison-table">
      <thead>
        <tr>
          <th>Company</th>
          <th>Monthly</th>
          <th>Annual</th>
          <th>Wage</th>
          <th>Bid @18%</th>
          <th>Bid @20%</th>
          <th>Hist. avg</th>
          <th>Competitive</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((r) => `
          <tr class="${r.is_selected ? "quote-row-selected" : ""}">
            <td>${escapeHtml(r.company_name)}</td>
            <td>${formatMoney(r.monthly_quote)}</td>
            <td>${formatMoney(r.annual_quote)}</td>
            <td><span class="wage-dot wage-${r.wage_compliance?.level || "neutral"}"></span></td>
            <td>${formatMoney(r.bid_at_18_margin)}</td>
            <td>${formatMoney(r.bid_at_20_margin)}</td>
            <td>${r.historical_avg_annual ? formatMoney(r.historical_avg_annual) : "—"}</td>
            <td><span class="comp-${r.competitiveness?.level || "neutral"}">${escapeHtml(r.competitiveness?.message || "")}</span></td>
            <td><button type="button" class="btn btn-small btn-secondary-action" data-select-contact="${r.id}">Select</button></td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderContractSubsPage(data) {
  const contacts = data.contacts || data.subs || [];
  const tabList = subsActiveTab === "list" ? "active" : "";
  const tabCompare = subsActiveTab === "compare" ? "active" : "";
  const listPane = subsActiveTab === "list"
    ? `<div class="subs-cards subs-workflow-grid">${contacts.map(renderContactCard).join("") || '<p class="empty">No subs linked yet. Run Google Places search or add from your network.</p>'}</div>`
    : "";
  const comparePane = subsActiveTab === "compare"
    ? renderQuoteComparisonTable(data.quote_comparison)
    : "";

  return `
    <div class="subs-page-header">
      <button type="button" class="btn btn-secondary-action btn-small" id="subs-back-btn">← Back to contract</button>
      <h2>${escapeHtml(data.contract_title || "Sub outreach")}</h2>
      <p class="detail-note">${escapeHtml(data.agency || "")}</p>
      <button type="button" class="btn btn-secondary-action btn-small" data-find-subs="${escapeHtml(data.notice_id)}">Run Google Places search</button>
    </div>
    ${renderWageBanner(data.wage_requirements)}
    ${renderPreBidChecklist(data.pre_bid_checklist, data.notice_id)}
    ${renderProgressTracker(data.progress)}
    <div class="subs-tab-bar">
      <button type="button" class="subs-tab ${tabList}" data-subs-tab="list">Sub list</button>
      <button type="button" class="subs-tab ${tabCompare}" data-subs-tab="compare">Quote comparison</button>
    </div>
    <div class="subs-tab-pane" data-pane="list" ${subsActiveTab !== "list" ? "hidden" : ""}>${listPane}</div>
    <div class="subs-tab-pane" data-pane="compare" ${subsActiveTab !== "compare" ? "hidden" : ""}>${comparePane}</div>
    <div id="sub-detail-modal" class="sub-modal" hidden></div>
    <div id="sub-voicemail-modal" class="sub-modal" hidden></div>
    <div id="sub-email-confirm-modal" class="sub-modal" hidden></div>`;
}

function bindContractSubsPage(container, noticeId) {
  if (container.id === "contract-detail-subs-root") {
    container.querySelector("#subs-back-btn")?.remove();
  } else {
    container.querySelector("#subs-back-btn")?.addEventListener("click", () => {
      stopContractSubsPolling();
      if (typeof openContractDetail === "function") openContractDetail(noticeId, "subs");
      else showView("dashboard");
    });
  }
  container.querySelectorAll("[data-subs-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      subsActiveTab = btn.dataset.subsTab;
      loadContractSubsInto(noticeId, container.id, { quiet: true });
    });
  });
    });
  });
  container.querySelectorAll(".sub-copy-phone").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const phone = btn.dataset.phone;
      if (phone) copyTextToClipboard(phone, "Phone copied.");
    });
  });
  container.querySelectorAll("[data-call-contact]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.callContact);
      await patchSubContact(id, { called: true, call_date: new Date().toISOString() });
      const phone = contractSubsData?.contacts?.find((c) => c.id === id)?.phone;
      if (phone) window.location.href = `tel:${phone.replace(/[^\d+]/g, "")}`;
    });
  });
  container.querySelectorAll("[data-log-quote]").forEach((btn) => {
    btn.addEventListener("click", () => openSubDetailModal(Number(btn.dataset.logQuote), { focusQuote: true }));
  });
  container.querySelectorAll("[data-view-contact]").forEach((btn) => {
    btn.addEventListener("click", () => openSubDetailModal(Number(btn.dataset.viewContact)));
  });
  container.querySelectorAll("[data-voicemail]").forEach((btn) => {
    btn.addEventListener("click", () => openVoicemailModal(Number(btn.dataset.voicemail)));
  });
  container.querySelectorAll("[data-select-contact]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await apiFetch(`/api/sub-contacts/${btn.dataset.selectContact}/select`, { method: "POST" });
      await loadContractSubsPage(noticeId, { quiet: true });
      showSyncStatus("Sub selected for proposal.");
    });
  });
  container.querySelector(`[data-bypass-checklist="${noticeId}"]`)?.addEventListener("click", async () => {
    if (!confirm("Bypass the pre-bid checklist? This will be logged.")) return;
    await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/sub-checklist/bypass`, { method: "POST" });
    await loadContractSubsPage(noticeId, { quiet: true });
    showSyncStatus("Checklist bypass logged.");
  });
}

async function patchSubContact(contactId, payload) {
  const res = await apiFetch(`/api/sub-contacts/${contactId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Save failed");
  if (activeContractSubsId) await loadContractSubsPage(activeContractSubsId, { quiet: true });
  return data;
}

async function openVoicemailModal(contactId) {
  const modal = document.getElementById("sub-voicemail-modal");
  if (!modal) return;
  const res = await apiFetch(`/api/sub-contacts/${contactId}/voicemail-script`);
  const data = await res.json();
  if (!res.ok) return;
  modal.hidden = false;
  modal.innerHTML = `
    <div class="sub-modal-backdrop" data-close-modal></div>
    <div class="sub-modal-panel">
      <h3>Voicemail script</h3>
      <p class="voicemail-script-text">${escapeHtml(data.script)}</p>
      <button type="button" class="btn btn-primary-action" data-copy-vm="${escapeHtml(data.script)}">Copy to Clipboard</button>
      <button type="button" class="btn btn-secondary-action" data-close-modal>Close</button>
    </div>`;
  modal.querySelector("[data-copy-vm]")?.addEventListener("click", (e) => {
    copyTextToClipboard(e.target.dataset.copyVm, "Voicemail script copied.");
  });
  modal.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", () => { modal.hidden = true; });
  });
}

async function openSubDetailModal(contactId, { focusQuote = false } = {}) {
  const modal = document.getElementById("sub-detail-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.innerHTML = `<div class="sub-modal-backdrop" data-close-modal></div><div class="sub-modal-panel sub-detail-panel"><p class="pricing-loading">Loading…</p></div>`;
  const res = await apiFetch(`/api/sub-contacts/${contactId}`);
  const contact = await res.json();
  if (!res.ok) {
    modal.querySelector(".sub-detail-panel").innerHTML = `<p>${escapeHtml(contact.detail || "Error")}</p>`;
    return;
  }
  modal.querySelector(".sub-detail-panel").innerHTML = renderSubDetailContent(contact, focusQuote);
  bindSubDetailModal(modal, contact);
  modal.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", () => { modal.hidden = true; });
  });
}

function renderSubDetailContent(c, focusQuote) {
  const refs = Array.isArray(c.references) ? c.references : [];
  while (refs.length < 3) refs.push({ client_name: "", contact: "", service: "", dates: "" });
  const wageCls = c.wage_compliance?.level || "neutral";
  return `
    <h2>${escapeHtml(c.company_name)}</h2>
    ${renderStatusBadge(c.status)}
    <section class="sub-detail-section">
      <h3>Contact information</h3>
      <div class="sub-detail-grid">
        <label>Company<input class="settings-input" data-field="company_name" value="${escapeHtml(c.company_name || "")}"></label>
        <label>Phone<div class="copy-field-row"><input class="settings-input" data-field="phone" value="${escapeHtml(c.phone || "")}"><button type="button" class="btn btn-small" data-copy-field="phone">Copy</button></div></label>
        <label>Email<div class="copy-field-row"><input class="settings-input" data-field="email" value="${escapeHtml(c.email || "")}"><button type="button" class="btn btn-small" data-copy-field="email">Copy</button></div></label>
        <label>Website<input class="settings-input" data-field="website" value="${escapeHtml(c.website || "")}"></label>
        <label>Address<input class="settings-input" data-field="address" value="${escapeHtml(c.address || "")}"></label>
        <label>City<input class="settings-input" data-field="city" value="${escapeHtml(c.city || "")}"></label>
        <label>State<input class="settings-input" data-field="state" value="${escapeHtml(c.state || "")}" maxlength="8"></label>
      </div>
    </section>
    <section class="sub-detail-section">
      <h3>Call log</h3>
      <label class="toggle-row"><input type="checkbox" data-field="called" ${c.called ? "checked" : ""}> Called</label>
      <label class="toggle-row"><input type="checkbox" data-field="reached" ${c.reached ? "checked" : ""}> Reached</label>
      <label class="toggle-row"><input type="checkbox" data-field="voicemail_left" ${c.voicemail_left ? "checked" : ""}> Voicemail left</label>
      <label>Notes<textarea class="settings-input" data-field="notes" rows="3">${escapeHtml(c.notes || "")}</textarea></label>
    </section>
    <section class="sub-detail-section">
      <h3>Email templates</h3>
      <p class="settings-help">Copy and send from your own email client — nothing is sent automatically.</p>
      <button type="button" class="btn btn-secondary-action btn-small" data-gen-scope="${c.id}">Generate Scope Email</button>
      <button type="button" class="btn btn-secondary-action btn-small" data-gen-followup="${c.id}">Generate Follow Up Email</button>
      <div id="email-template-box" hidden>
        <button type="button" class="btn btn-primary-action btn-small" data-copy-email-top>Copy to Clipboard</button>
        <textarea id="email-template-text" class="settings-input email-template-area" rows="16"></textarea>
        <button type="button" class="btn btn-primary-action btn-small" data-copy-email-bottom>Copy to Clipboard</button>
        <ol class="email-after-copy-checklist">
          <li>Open your email client</li>
          <li>Paste the email text</li>
          <li>Add the sub's email in the To field</li>
          <li>Review and edit as needed</li>
          <li>Send</li>
        </ol>
      </div>
    </section>
    <section class="sub-detail-section" id="quote-section">
      <h3>Quote</h3>
      <label class="toggle-row"><input type="checkbox" data-field="quote_received" ${c.quote_received ? "checked" : ""}> Quote received</label>
      <label>Monthly quote ($)<input type="number" class="settings-input ${focusQuote ? "focus-field" : ""}" data-field="quote_amount" step="0.01" value="${c.quote_amount ?? ""}"></label>
      <p class="wage-compliance-indicator wage-${wageCls}" data-wage-indicator>${escapeHtml(c.wage_compliance?.message || "")}</p>
      <p>Annual quote: <strong data-annual-quote>${c.annual_quote != null ? formatMoney(c.annual_quote) : "—"}</strong></p>
      <label class="toggle-row"><input type="checkbox" data-field="payment_terms_confirmed" ${c.payment_terms_confirmed ? "checked" : ""}> Payment terms confirmed (Net 45)</label>
    </section>
    <section class="sub-detail-section">
      <h3>References</h3>
      <label class="toggle-row"><input type="checkbox" data-field="references_requested" ${c.references_requested ? "checked" : ""}> References requested</label>
      <label class="toggle-row"><input type="checkbox" data-field="references_received" ${c.references_received ? "checked" : ""}> References received</label>
      ${refs.map((r, i) => `
        <div class="reference-block" data-ref-idx="${i}">
          <label>Client name<input class="settings-input" data-ref="client_name" value="${escapeHtml(r.client_name || "")}"></label>
          <label>Phone or email<input class="settings-input" data-ref="contact" value="${escapeHtml(r.contact || "")}"></label>
          <label>Service performed<input class="settings-input" data-ref="service" value="${escapeHtml(r.service || "")}"></label>
          <label>Dates<input class="settings-input" data-ref="dates" value="${escapeHtml(r.dates || "")}"></label>
        </div>`).join("")}
    </section>
    <section class="sub-detail-section">
      <h3>Insurance</h3>
      <label class="toggle-row"><input type="checkbox" data-field="insurance_verified" ${c.insurance_verified ? "checked" : ""}> Insurance verified</label>
      <label>Expiration date<input type="date" class="settings-input" data-field="insurance_expiration_date" value="${(c.insurance_expiration_date || "").slice(0, 10)}"></label>
      <label>Coverage amount<input type="number" class="settings-input" data-field="insurance_coverage_amount" step="0.01" value="${c.insurance_coverage_amount ?? ""}"></label>
    </section>
    <section class="sub-detail-section">
      <h3>Selection</h3>
      ${c.is_selected
        ? `<button type="button" class="btn btn-secondary-action" data-deselect="${c.id}">Deselect</button>`
        : `<button type="button" class="btn btn-primary-action" data-select="${c.id}">Select This Sub</button>`}
    </section>
    <button type="button" class="btn btn-secondary-action" data-close-modal>Close</button>`;
}

function bindSubDetailModal(modal, contact) {
  const panel = modal.querySelector(".sub-detail-panel");
  const collectPayload = () => {
    const payload = {};
    panel.querySelectorAll("[data-field]").forEach((el) => {
      const field = el.dataset.field;
      if (el.type === "checkbox") payload[field] = el.checked;
      else if (field === "quote_amount" || field === "insurance_coverage_amount") {
        payload[field] = el.value ? Number(el.value) : null;
      } else payload[field] = el.value || null;
    });
    const refs = [];
    panel.querySelectorAll(".reference-block").forEach((block) => {
      const ref = {};
      block.querySelectorAll("[data-ref]").forEach((el) => { ref[el.dataset.ref] = el.value; });
      refs.push(ref);
    });
    payload.references = refs;
    return payload;
  };
  const save = debounce(() => {
    patchSubContact(contact.id, collectPayload()).catch((err) => showSyncStatus(err.message, true));
  }, 500);
  panel.querySelectorAll("[data-field], [data-ref]").forEach((el) => {
    el.addEventListener("change", save);
    el.addEventListener("input", save);
  });
  panel.querySelector("[data-field=quote_amount]")?.addEventListener("input", (e) => {
    const monthly = Number(e.target.value);
    const annual = panel.querySelector("[data-annual-quote]");
    if (annual && monthly) annual.textContent = formatMoney(monthly * 12);
  });
  panel.querySelectorAll("[data-copy-field]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const field = btn.dataset.copyField;
      const val = panel.querySelector(`[data-field="${field}"]`)?.value;
      if (val) copyTextToClipboard(val, "Copied.");
    });
  });
  const showEmail = async (url, contactId) => {
    const res = await apiFetch(url);
    const data = await res.json();
    if (!res.ok) return;
    const box = panel.querySelector("#email-template-box");
    const ta = panel.querySelector("#email-template-text");
    if (!box || !ta) return;
    box.hidden = false;
    ta.value = data.body;
    const copyHandler = () => {
      copyTextToClipboard(ta.value, "Copied — paste into your email client and send.");
      confirmEmailSent(contactId);
    };
    panel.querySelector("[data-copy-email-top]")?.addEventListener("click", copyHandler);
    panel.querySelector("[data-copy-email-bottom]")?.addEventListener("click", copyHandler);
  };
  panel.querySelector("[data-gen-scope]")?.addEventListener("click", () => {
    showEmail(`/api/sub-contacts/${contact.id}/scope-email`, contact.id);
  });
  panel.querySelector("[data-gen-followup]")?.addEventListener("click", () => {
    showEmail(`/api/sub-contacts/${contact.id}/followup-email`, contact.id);
  });
  panel.querySelector("[data-select]")?.addEventListener("click", async () => {
    await apiFetch(`/api/sub-contacts/${contact.id}/select`, { method: "POST" });
    modal.hidden = true;
    if (activeContractSubsId) await loadContractSubsPage(activeContractSubsId, { quiet: true });
  });
  panel.querySelector("[data-deselect]")?.addEventListener("click", async () => {
    await apiFetch(`/api/sub-contacts/${contact.id}/deselect`, { method: "POST" });
    modal.hidden = true;
    if (activeContractSubsId) await loadContractSubsPage(activeContractSubsId, { quiet: true });
  });
}

function confirmEmailSent(contactId) {
  const modal = document.getElementById("sub-email-confirm-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.innerHTML = `
    <div class="sub-modal-backdrop" data-close-email-confirm></div>
    <div class="sub-modal-panel">
      <p>Did you send this email? Mark as sent?</p>
      <button type="button" class="btn btn-primary-action" data-email-yes>Yes</button>
      <button type="button" class="btn btn-secondary-action" data-email-no>No</button>
    </div>`;
  modal.querySelector("[data-email-yes]")?.addEventListener("click", async () => {
    await apiFetch(`/api/sub-contacts/${contactId}/mark-email-sent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sent: true }),
    });
    modal.hidden = true;
    showSyncStatus("Email marked as sent.");
  });
  modal.querySelector("[data-email-no]")?.addEventListener("click", () => { modal.hidden = true; });
  modal.querySelector("[data-close-email-confirm]")?.addEventListener("click", () => { modal.hidden = true; });
}

const AGREEMENT_SIGNATURE_STATUSES = [
  "Agreement Not Generated",
  "Agreement Sent",
  "Agreement Signed",
  "Agreement Declined",
];

function renderSubProfileFields(sub) {
  return `
    <div class="sub-agreement-profile">
      <label class="filter-label">Owner / representative</label>
      <input type="text" class="settings-input sub-profile-field" data-profile="owner_name" value="${escapeHtml(sub.owner_name || "")}" placeholder="Legal signatory name">
      <label class="filter-label">Owner title</label>
      <input type="text" class="settings-input sub-profile-field" data-profile="owner_title" value="${escapeHtml(sub.owner_title || "")}" placeholder="Owner">
      <label class="filter-label">Street address</label>
      <input type="text" class="settings-input sub-profile-field" data-profile="address" value="${escapeHtml(sub.address || "")}">
      <div class="sub-profile-row">
        <div><label class="filter-label">City</label><input type="text" class="settings-input sub-profile-field" data-profile="city" value="${escapeHtml(sub.city || "")}"></div>
        <div><label class="filter-label">State</label><input type="text" class="settings-input sub-profile-field" data-profile="state" value="${escapeHtml(sub.state || "")}" maxlength="8"></div>
        <div><label class="filter-label">ZIP</label><input type="text" class="settings-input sub-profile-field" data-profile="zip" value="${escapeHtml(sub.zip || "")}"></div>
      </div>
      <label class="filter-label">License number</label>
      <input type="text" class="settings-input sub-profile-field" data-profile="license_number" value="${escapeHtml(sub.license_number || "")}">
      <label class="filter-label">Insurance carrier</label>
      <input type="text" class="settings-input sub-profile-field" data-profile="insurance_carrier" value="${escapeHtml(sub.insurance_carrier || "")}">
      <label class="filter-label">Sub email</label>
      <input type="email" class="settings-input sub-profile-field" data-profile="business_email" value="${escapeHtml(sub.business_email || "")}">
    </div>`;
}

function renderSelectChecklist(sub) {
  const checks = [
    { ok: Boolean(sub.quote_amount), label: "Quote amount entered" },
    { ok: Boolean(sub.owner_name), label: "Owner / representative name" },
    { ok: Boolean(sub.license_number), label: "License number" },
    { ok: Boolean(sub.insurance_carrier), label: "Insurance carrier" },
    { ok: Boolean(sub.business_email), label: "Sub email for notices" },
    { ok: Boolean(sub.address), label: "Street address" },
  ];
  const ready = checks.every((c) => c.ok);
  return `
    <ul class="sub-select-checklist">
      ${checks
        .map(
          (c) =>
            `<li class="${c.ok ? "check-ok" : "check-missing"}">${c.ok ? "✓" : "○"} ${escapeHtml(c.label)}</li>`
        )
        .join("")}
    </ul>
    ${ready ? "" : `<p class="detail-note">Complete checklist before generating agreement.</p>`}`;
}

function renderAgreementSection(sub) {
  const agreement = sub.agreement || {};
  const hasAgreement = agreement.has_agreement;
  const status = sub.agreement_signature_status || agreement.agreement_signature_status || "Agreement Not Generated";
  const missing = agreement.missing_fields || [];
  const blockGenerate = missing.length > 0;
  const missingNote =
    missing.length
      ? `<p class="detail-note agreement-missing-note">Required before generate: ${escapeHtml(missing.map((m) => m.label).join(", "))}</p>`
      : "";
  const generatedNote = hasAgreement && agreement.generated_at
    ? `<p class="detail-note">Generated ${new Date(agreement.generated_at).toLocaleString()} (v${agreement.version || 1})</p>`
    : "";
  const log = sub.agreement_status_log || agreement.agreement_status_log || [];
  const logHtml = log.length
    ? `<ul class="agreement-status-log">${log
        .slice()
        .reverse()
        .slice(0, 5)
        .map(
          (e) =>
            `<li>${escapeHtml(e.status)} · ${e.at ? new Date(e.at).toLocaleString() : ""}${e.note ? ` — ${escapeHtml(e.note)}` : ""}</li>`
        )
        .join("")}</ul>`
    : "";
  const disabled = blockGenerate ? "disabled" : "";
  const actions = hasAgreement
    ? `<button type="button" class="btn btn-secondary-action btn-small sub-agreement-resend" data-link-id="${sub.id}" ${disabled}>Resend Agreement</button>
       <button type="button" class="btn btn-secondary-action btn-small sub-agreement-download" data-link-id="${sub.id}">Download PDF</button>`
    : `<button type="button" class="btn btn-primary-action btn-small sub-agreement-generate" data-link-id="${sub.id}" ${disabled}>Generate Subcontract Agreement</button>`;
  return `
    <div class="sub-agreement-section">
      <p class="card-label">Subcontract agreement</p>
      ${renderSelectChecklist(sub)}
      ${missingNote}
      ${generatedNote}
      ${logHtml}
      <label class="filter-label">Signature status</label>
      <select class="settings-input sub-agreement-status" data-field="agreement_signature_status">
        ${AGREEMENT_SIGNATURE_STATUSES.map(
          (s) => `<option value="${escapeHtml(s)}" ${s === status ? "selected" : ""}>${escapeHtml(s)}</option>`
        ).join("")}
      </select>
      <div class="sub-agreement-actions">${actions}</div>
      <p class="sub-agreement-status-msg" data-agreement-msg hidden></p>
    </div>`;
}

function bindContractSubCards(container) {
  container.querySelectorAll(".sub-card").forEach((card) => {
    const linkId = card.dataset.linkId;
    const subId = card.dataset.subId;
    const save = debounceSubSave(linkId, card);
    card.querySelectorAll("[data-field]").forEach((el) => {
      el.addEventListener("change", save);
      if (el.tagName === "TEXTAREA" || el.type === "number" || el.type === "date") {
        el.addEventListener("input", save);
      }
    });
    const profileSave = debounceSubProfileSave(subId, card);
    card.querySelectorAll("[data-profile]").forEach((el) => {
      el.addEventListener("change", profileSave);
      el.addEventListener("input", profileSave);
    });
    card.querySelector(".sub-agreement-generate")?.addEventListener("click", () => {
      generateSubAgreement(linkId, card, false).catch((err) => showSyncStatus(err.message, true));
    });
    card.querySelector(".sub-agreement-resend")?.addEventListener("click", () => {
      generateSubAgreement(linkId, card, true).catch((err) => showSyncStatus(err.message, true));
    });
    card.querySelector(".sub-agreement-download")?.addEventListener("click", () => {
      downloadSubAgreementPdf(linkId).catch((err) => showSyncStatus(err.message, true));
    });
  });
  const back = container.querySelector("#subs-back-btn") || document.getElementById("subs-back-btn");
  if (back) {
    back.addEventListener("click", () => {
      stopContractSubsPolling();
      showView("dashboard");
      if (activeContractSubsId) openDetail(activeContractSubsId);
    });
  }
}

const _subSaveTimers = new Map();
const _subProfileSaveTimers = new Map();

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function debounceSubProfileSave(subId, card) {
  return () => {
    clearTimeout(_subProfileSaveTimers.get(subId));
    _subProfileSaveTimers.set(
      subId,
      setTimeout(() => patchSubProfile(subId, card), 500)
    );
  };
}

async function patchSubProfile(subId, card) {
  const payload = {};
  card.querySelectorAll("[data-profile]").forEach((el) => {
    payload[el.dataset.profile] = el.value.trim() || null;
  });
  const res = await apiFetch(`/api/subs/${subId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    showSyncStatus(data.detail || "Could not save sub profile", true);
    return;
  }
  if (activeContractSubsId) {
    await loadContractSubsPage(activeContractSubsId, { quiet: true });
  }
}

async function generateSubAgreement(linkId, card, resend) {
  const btn = card.querySelector(resend ? ".sub-agreement-resend" : ".sub-agreement-generate");
  if (btn?.disabled) {
    showSyncStatus("Fill all required sub profile and contract fields before generating.", true);
    return;
  }
  const msg = card.querySelector("[data-agreement-msg]");
  if (btn) {
    btn.disabled = true;
    btn.textContent = resend ? "Regenerating…" : "Generating…";
  }
  if (msg) {
    msg.hidden = false;
    msg.textContent = "Claude is filling the agreement — this may take a minute…";
  }
  showSyncStatus(resend ? "Regenerating subcontract agreement…" : "Generating subcontract agreement with Claude…");
  const url = resend
    ? `/api/contract-subs/${linkId}/agreement/resend`
    : `/api/contract-subs/${linkId}/agreement/generate`;
  const res = await apiFetch(url, { method: "POST" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Agreement generation failed");
  showSyncStatus(resend ? "Subcontract agreement regenerated and saved." : "Subcontract agreement generated — download PDF or update signature status.");
  if (activeContractSubsId) await loadContractSubsPage(activeContractSubsId, { quiet: true });
}

async function downloadSubAgreementPdf(linkId) {
  const res = await apiFetch(`/api/contract-subs/${linkId}/agreement/pdf`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || "Download failed");
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : "SubcontractAgreement.pdf";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  showSyncStatus("Agreement PDF downloaded.");
}

function debounceSubSave(linkId, card) {
  return () => {
    clearTimeout(_subSaveTimers.get(linkId));
    _subSaveTimers.set(
      linkId,
      setTimeout(() => patchContractSub(linkId, card), 400)
    );
  };
}

async function patchContractSub(linkId, card) {
  const statusEl = card.querySelector('[data-field="status"]');
  const prevStatus = statusEl?.value;
  const payload = {};
  card.querySelectorAll("[data-field]").forEach((el) => {
    const field = el.dataset.field;
    if (field === "quote_amount") payload.quote_amount = el.value ? Number(el.value) : null;
    else if (field === "quote_date") payload.quote_date = el.value || null;
    else payload[field] = el.value;
  });
  const res = await apiFetch(`/api/contract-subs/${linkId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    showSyncStatus(data.detail || "Could not save sub update", true);
    return;
  }
  const hint = card.querySelector("[data-save-hint]");
  if (hint) {
    hint.hidden = false;
    clearTimeout(hint._hideTimer);
    hint._hideTimer = setTimeout(() => {
      hint.hidden = true;
    }, 1500);
  }
  const statusChanged = data.status !== prevStatus;
  if (statusChanged && (data.status === "Selected" || prevStatus === "Selected")) {
    if (activeContractSubsId) {
      await loadContractSubsPage(activeContractSubsId, { quiet: true });
      await loadContracts();
    }
    return;
  }
  card.classList.toggle("sub-card-selected", data.is_selected);
}

function truncateNotes(text, max = 72) {
  if (!text) return "—";
  const clean = String(text).replace(/<!--[\s\S]*?-->/g, " ").replace(/\s+/g, " ").trim();
  if (!clean) return "—";
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

async function loadMySubsPage() {
  const search = document.getElementById("subs-search")?.value.trim() || "";
  const subType = document.getElementById("subs-type-filter")?.value || "";
  const state = document.getElementById("subs-state-filter")?.value.trim() || "";
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (subType) params.set("sub_type", subType);
  if (state) params.set("state", state);
  const res = await apiFetch(`/api/subs?${params.toString()}`);
  const data = await res.json();
  mySubsCache = data.subs || [];
  renderMySubsTable(mySubsCache);
}

function renderMySubsTable(subs) {
  const tbody = document.getElementById("my-subs-body");
  if (!tbody) return;
  if (!subs.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">No subs in your database yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = subs
    .map(
      (s) => `
    <tr data-sub-id="${s.id}" class="my-subs-row">
      <td>${escapeHtml(s.business_name)}</td>
      <td>${escapeHtml(s.sub_type || "—")}</td>
      <td>${escapeHtml([s.city, s.state].filter(Boolean).join(", ") || "—")}</td>
      <td>${s.rating != null ? `${s.rating} (${s.review_count ?? 0})` : "—"}</td>
      <td>${escapeHtml(s.phone || "—")}</td>
      <td class="my-subs-notes">${escapeHtml(truncateNotes(s.latest_outreach_notes || s.notes))}</td>
      <td>${s.times_contacted ?? 0}</td>
      <td>${s.times_selected ?? 0}</td>
      <td>${s.last_contacted_at ? new Date(s.last_contacted_at).toLocaleDateString() : "—"}</td>
    </tr>`
    )
    .join("");
  tbody.querySelectorAll(".my-subs-row").forEach((row) => {
    row.addEventListener("click", () => openSubHistory(Number(row.dataset.subId)));
  });
}

async function openSubHistory(subId) {
  const panel = document.getElementById("sub-history-panel");
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = `<p class="pricing-loading">Loading history…</p>`;
  const res = await apiFetch(`/api/subs/${subId}`);
  const data = await res.json();
  if (!res.ok) {
    panel.innerHTML = `<p>${escapeHtml(data.detail || "Error")}</p>`;
    return;
  }
  const sub = data.sub || {};
  panel.innerHTML = `
    <h3>${escapeHtml(sub.business_name)}</h3>
    <p>${escapeHtml(sub.address || [sub.city, sub.state].filter(Boolean).join(", ") || "")}</p>
    <p class="settings-help">Saved in your master sub database — reuse when a nearby contract comes up.</p>
    <label class="filter-label">All saved notes</label>
    <textarea id="master-sub-notes" class="settings-input" rows="5">${escapeHtml(sub.notes || "")}</textarea>
    <button type="button" class="btn btn-secondary-action btn-small" id="save-master-sub-notes">Save notes</button>
    <h4>Contract history</h4>
    <ul class="detail-list">${(data.history || [])
      .map(
        (h) =>
          `<li><strong>${escapeHtml(h.contract_title || h.contract_notice_id || "Contract")}</strong> — ${escapeHtml(h.status)}${h.quote_amount ? ` · ${formatMoney(h.quote_amount)}` : ""}${h.contact_notes ? `<br><span class="sub-history-notes">${escapeHtml(h.contact_notes)}</span>` : ""}</li>`
      )
      .join("") || "<li>No contract outreach yet.</li>"}</ul>
    <button type="button" class="btn btn-secondary-action btn-small" id="close-sub-history">Close</button>`;
  document.getElementById("save-master-sub-notes")?.addEventListener("click", async () => {
    const notes = document.getElementById("master-sub-notes").value;
    await apiFetch(`/api/subs/${subId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes }),
    });
    showSyncStatus("Sub notes saved.");
  });
  document.getElementById("close-sub-history")?.addEventListener("click", () => {
    panel.hidden = true;
  });
}

async function createManualSub(event) {
  event.preventDefault();
  const form = event.target;
  const body = {
    business_name: form.business_name.value.trim(),
    phone: form.phone.value.trim() || null,
    city: form.city.value.trim() || null,
    state: form.state.value.trim().toUpperCase() || null,
    sub_type: form.sub_type.value.trim() || null,
    website: form.website.value.trim() || null,
    notes: form.notes.value.trim() || null,
  };
  const res = await apiFetch("/api/subs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Could not add sub");
  form.reset();
  showSyncStatus("Sub added to your database.");
  await loadMySubsPage();
}

function bindSubFinderEvents() {
  document.getElementById("subs-search-btn")?.addEventListener("click", loadMySubsPage);
  document.getElementById("manual-sub-form")?.addEventListener("submit", (e) => {
    createManualSub(e).catch((err) => showSyncStatus(err.message, true));
  });

  document.body.addEventListener("click", (e) => {
    const findBtn = e.target.closest("[data-find-subs]");
    if (findBtn) {
      e.preventDefault();
      e.stopPropagation();
      findSubs(findBtn.dataset.findSubs, { force: true }).catch((err) => showSyncStatus(err.message, true));
      return;
    }
    const openSubs = e.target.closest("[data-open-subs]");
    if (openSubs) {
      e.preventDefault();
      e.stopPropagation();
      openContractSubs(openSubs.dataset.openSubs);
      return;
    }
    const addNet = e.target.closest("[data-add-network]");
    if (addNet) {
      e.preventDefault();
      e.stopPropagation();
      addNetworkSubs(addNet.dataset.addNetwork).catch((err) => showSyncStatus(err.message, true));
    }
  });
}

document.addEventListener("DOMContentLoaded", bindSubFinderEvents);
