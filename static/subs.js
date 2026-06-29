/** Sub Finder UI — contract subs, master database, outreach tracking. */

const SUB_STATUSES = [
  "Not Contacted",
  "Called — Left Voicemail",
  "Spoke With — Interested",
  "Spoke With — Not Interested",
  "Quote Received",
  "Selected",
];

const AGREEMENT_SIGNATURE_STATUSES = [
  "Agreement Not Generated",
  "Agreement Sent",
  "Agreement Signed",
  "Agreement Declined",
];

let activeContractSubsId = null;
let contractSubsPollTimer = null;
let mySubsCache = [];

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
  activeContractSubsId = noticeId;
  showView("contract-subs");
  loadContractSubsPage(noticeId);
}

function stopContractSubsPolling() {
  if (contractSubsPollTimer) {
    clearInterval(contractSubsPollTimer);
    contractSubsPollTimer = null;
  }
}

function startContractSubsPolling(noticeId) {
  stopContractSubsPolling();
  contractSubsPollTimer = setInterval(() => {
    if (activeContractSubsId !== noticeId) {
      stopContractSubsPolling();
      return;
    }
    loadContractSubsPage(noticeId, { quiet: true });
  }, 4000);
}

async function loadContractSubsPage(noticeId, { quiet = false } = {}) {
  const container = document.getElementById("contract-subs-content");
  if (!container) return;
  if (!quiet) container.innerHTML = `<p class="pricing-loading">Loading subs…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/subs`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load subs");
    if (data.summary?.status === "searching") startContractSubsPolling(noticeId);
    else stopContractSubsPolling();
    container.innerHTML = renderContractSubsPage(data);
    bindContractSubCards(container);
  } catch (err) {
    if (!quiet) container.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderContractSubsPage(data) {
  const summary = data.summary || {};
  const city = summary.city || "this area";
  const radius = summary.radius_miles || 25;
  const header = summary.recommended_count
    ? `${summary.recommended_count} recommended subs within ${radius} miles of ${city}`
    : data.subs?.length
    ? `${data.subs.length} subs for this contract`
    : "No subs found automatically. You can add subs manually from My Subs.";
  const selectedNote = data.selected_sub_quote
    ? `<p class="detail-note selected-quote-note">This sub's quote (${formatMoney(data.selected_sub_quote)}) will be used in your proposal.</p>`
    : "";

  return `
    <div class="subs-page-header">
      <button type="button" class="btn btn-secondary-action btn-small" id="subs-back-btn">← Back to contract</button>
      <h2>${escapeHtml(data.contract_title || "Contract subs")}</h2>
      <p class="detail-note">${escapeHtml(data.agency || "")}</p>
      <p class="pricing-intro">${escapeHtml(header)}</p>
      ${selectedNote}
      <button type="button" class="btn btn-secondary-action btn-small" data-find-subs="${escapeHtml(data.notice_id)}">Run Google Places search</button>
    </div>
    <div class="subs-cards subs-workflow-grid">${(data.subs || []).map(renderSubCard).join("") || '<p class="empty">No subs linked yet.</p>'}</div>`;
}

function renderSubCard(sub) {
  const phoneLink = sub.phone
    ? `<a href="tel:${escapeHtml(String(sub.phone).replace(/[^\d+]/g, ""))}">${escapeHtml(sub.phone)}</a>`
    : "—";
  const stars = sub.rating != null ? `${sub.rating} ★ (${sub.review_count ?? 0} reviews)` : "No rating";
  const agreementSection = sub.status === "Selected" ? renderAgreementSection(sub) : "";
  const profileSection = sub.status === "Selected" ? renderSubProfileFields(sub) : "";
  const selectedClass = sub.is_selected ? " sub-card-selected sub-card-workflow-active" : "";

  const outreachBlock = `
    <div class="sub-workflow-step">
      <div class="sub-workflow-step-head"><span class="sub-workflow-num">1</span> Outreach</div>
      <div class="sub-outreach-grid">
        <div class="sub-outreach-field sub-outreach-field-full">
          <label class="filter-label">Status</label>
          <select class="settings-input sub-status-select" data-field="status">
            ${SUB_STATUSES.map((s) => `<option value="${escapeHtml(s)}" ${s === sub.status ? "selected" : ""}>${escapeHtml(s)}</option>`).join("")}
          </select>
        </div>
        <div class="sub-outreach-field">
          <label class="filter-label">Monthly quote</label>
          <input type="number" class="settings-input sub-quote-amount" data-field="quote_amount" value="${sub.quote_amount ?? ""}" step="0.01" placeholder="Amount">
        </div>
        <div class="sub-outreach-field">
          <label class="filter-label">Quote date</label>
          <input type="date" class="settings-input sub-quote-date" data-field="quote_date" value="${sub.quote_date || ""}">
        </div>
        <div class="sub-outreach-field sub-outreach-field-full">
          <label class="filter-label">Call notes</label>
          <textarea class="settings-input sub-notes" data-field="contact_notes" rows="2" placeholder="Who you spoke with…">${escapeHtml(sub.contact_notes || "")}</textarea>
        </div>
      </div>
    </div>`;

  const agreementBlock =
    sub.status === "Selected"
      ? `<div class="sub-workflow-columns">
          <div class="sub-workflow-step">
            <div class="sub-workflow-step-head"><span class="sub-workflow-num">2</span> Agreement info</div>
            ${profileSection.replace('class="sub-agreement-profile"', 'class="sub-agreement-profile sub-workflow-panel"')}
          </div>
          <div class="sub-workflow-step">
            <div class="sub-workflow-step-head"><span class="sub-workflow-num">3</span> Generate &amp; send</div>
            ${agreementSection.replace('class="sub-agreement-section"', 'class="sub-agreement-section sub-workflow-panel"')}
          </div>
        </div>`
      : "";

  return `
    <article class="sub-card${selectedClass}" data-link-id="${sub.id}" data-sub-id="${sub.sub_id}">
      <header class="sub-card-header">
        <div>
          <h3 class="sub-card-title">${escapeHtml(sub.business_name || "Unknown")}</h3>
          <p class="sub-card-meta">${escapeHtml(stars)} · ${sub.distance_miles != null ? `${sub.distance_miles} mi` : "—"} · ${phoneLink}</p>
        </div>
        <div class="sub-card-links">
          ${sub.website ? `<a href="${escapeHtml(sub.website)}" target="_blank" rel="noopener">Website</a>` : ""}
          ${sub.google_maps_url ? `<a href="${escapeHtml(sub.google_maps_url)}" target="_blank" rel="noopener">Maps</a>` : ""}
        </div>
      </header>
      <p class="sub-card-claude"><strong>Claude ${sub.claude_score ?? "—"}/10</strong> — ${escapeHtml(sub.claude_reason || "Not analyzed")}</p>
      <div class="sub-workflow-body">
        ${outreachBlock}
        ${agreementBlock}
      </div>
      ${sub.status === "Selected" ? `<p class="detail-note selected-quote-note sub-card-selected-note">Selected for proposal &amp; subcontract agreement.</p>` : ""}
      <p class="sub-save-hint" data-save-hint hidden>Saved</p>
    </article>`;
}

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
  document.getElementById("tab-subs")?.addEventListener("click", () => {
    stopContractSubsPolling();
    showView("subs");
    loadMySubsPage();
  });
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
