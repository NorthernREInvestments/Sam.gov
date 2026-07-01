/** Contract performance, invoicing, and payment tracking UI. */

const INVOICE_STATUSES = ["Not Started", "Submitted", "Accepted", "Paid", "Rejected", "Overdue"];
const INVOICING_SYSTEMS = ["WAWF", "IPP", "Email", "Paper Check", "Other"];
const PAYMENT_METHODS = ["ACH", "Check", "Wire", "Other"];
const CPARS_RATINGS = ["Pending", "Exceptional", "Very Good", "Satisfactory", "Marginal", "Unsatisfactory"];
const PERF_STATUSES = [
  { value: "awarded", label: "Awarded" },
  { value: "active", label: "Active" },
  { value: "option_year", label: "Option Year" },
  { value: "stop_work", label: "Stop Work" },
  { value: "completed", label: "Completed" },
  { value: "not_awarded", label: "Not Awarded" },
];

function statusBadgeClass(status) {
  const s = (status || "").toLowerCase();
  if (s === "paid" || s === "active" || s === "awarded") return "perf-badge-green";
  if (s === "overdue" || s === "rejected" || s === "stop_work") return "perf-badge-red";
  if (s === "submitted" || s === "accepted" || s === "pending signoff") return "perf-badge-yellow";
  return "perf-badge-neutral";
}

function renderPerformanceAlertsBanners(alerts) {
  if (!alerts?.length) return "";
  return alerts
    .map(
      (a) =>
        `<div class="perf-banner perf-banner-${a.level || "red"}" data-alert-type="${escapeHtml(a.type || "")}">${escapeHtml(a.message)}</div>`,
    )
    .join("");
}

async function loadDashboardPerformanceAlerts() {
  const mount = document.getElementById("dashboard-perf-alerts");
  if (!mount) return;
  try {
    const res = await apiFetch("/api/performance/alerts");
    const data = await res.json();
    const parts = [];
    const wawf = data.wawf_warning;
    if (wawf?.message) {
      parts.push(`<div class="perf-banner perf-banner-${wawf.level === "red" ? "red" : "yellow"}">${escapeHtml(wawf.message)}</div>`);
    }
    const ipp = data.ipp_reminder;
    if (ipp?.message) {
      parts.push(
        `<div class="perf-banner perf-banner-yellow perf-banner-dismissible" id="ipp-reminder-banner">
          <span>${escapeHtml(ipp.message)}</span>
          <button type="button" class="btn btn-secondary-action btn-sm" id="ipp-mark-registered">Mark complete</button>
        </div>`,
      );
    }
    mount.innerHTML = parts.join("");
    document.getElementById("ipp-mark-registered")?.addEventListener("click", async () => {
      await apiFetch("/api/settings/performance", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ipp_registered: true }),
      });
      loadDashboardPerformanceAlerts();
    });
  } catch {
    /* ignore */
  }
}

function renderCardPerformanceBanners(c) {
  const alerts = c.performance_alerts || [];
  if (c.payment_overdue_alert?.message) {
    const exists = alerts.some((a) => a.type === "payment_overdue");
    if (!exists) {
      alerts.push({ type: "payment_overdue", level: "red", message: c.payment_overdue_alert.message });
    }
  }
  return renderPerformanceAlertsBanners(alerts);
}

async function loadPerformanceDashboard() {
  const mount = document.getElementById("performance-dashboard-content");
  if (!mount) return;
  mount.innerHTML = `<p class="settings-help">Loading performance dashboard…</p>`;
  try {
    const res = await apiFetch("/api/performance/dashboard");
    const data = await res.json();
    mount.innerHTML = renderPerformanceDashboard(data);
    mount.querySelectorAll("[data-open-contract]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        if (typeof openContractDetail === "function") openContractDetail(btn.dataset.openContract);
        else if (typeof openDetail === "function") openDetail(btn.dataset.openContract);
      });
    });
  } catch (err) {
    mount.innerHTML = `<p class="settings-help perf-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderPerformanceDashboard(data) {
  const s = data.summary || {};
  const cf = data.cash_flow || {};
  const wawf = s.wawf_warning || {};
  const ipp = s.ipp_reminder;
  let headerAlerts = "";
  if (wawf.message) {
    headerAlerts += `<div class="perf-banner perf-banner-${wawf.level === "red" ? "red" : "yellow"}">${escapeHtml(wawf.message)}</div>`;
  }
  if (ipp?.message) {
    headerAlerts += `<div class="perf-banner perf-banner-yellow">${escapeHtml(ipp.message)}</div>`;
  }
  const contractRows = (data.contracts || [])
    .map((c) => {
      const urg = c.urgency || "green";
      return `<tr class="perf-row-${urg}">
        <td><button type="button" class="link-btn" data-open-contract="${escapeHtml(c.notice_id)}">${escapeHtml(c.title || c.notice_id)}</button></td>
        <td>${escapeHtml(c.status || "")}</td>
        <td><span class="perf-badge ${statusBadgeClass(c.current_invoice_status)}">${escapeHtml(c.current_invoice_status)}</span></td>
        <td>${c.payment_overdue ? '<span class="perf-badge perf-badge-red">Overdue</span>' : "—"}</td>
      </tr>`;
    })
    .join("");
  return `
    <h2>Performance</h2>
    <p class="settings-help">Post-award invoicing, sub payments, and cash flow across active contracts.</p>
    ${headerAlerts}
    <section class="perf-section">
      <h3>Active contracts summary</h3>
      <div class="perf-stat-grid">
        <div class="perf-stat"><span class="perf-stat-label">Active contracts</span><span class="perf-stat-value">${s.active_contracts ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Monthly revenue expected</span><span class="perf-stat-value">${formatMoney(s.monthly_revenue_expected)}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Invoices submitted (month)</span><span class="perf-stat-value">${s.invoices_submitted_this_month ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Payments received (month)</span><span class="perf-stat-value">${s.payments_received_this_month ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Sub payments due (month)</span><span class="perf-stat-value">${s.sub_payments_due_this_month ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Overdue invoices</span><span class="perf-stat-value ${s.overdue_invoices ? "perf-stat-alert" : ""}">${s.overdue_invoices ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Overdue sub payments</span><span class="perf-stat-value ${s.overdue_sub_payments ? "perf-stat-alert" : ""}">${s.overdue_sub_payments ?? 0}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Amendment alerts</span><span class="perf-stat-value ${s.amendment_alerts ? "perf-stat-alert" : ""}">${s.amendment_alerts ?? 0}</span></div>
      </div>
    </section>
    <section class="perf-section">
      <h3>Monthly cash flow</h3>
      <div class="perf-stat-grid perf-stat-grid-3">
        <div class="perf-stat"><span class="perf-stat-label">Gov payments received</span><span class="perf-stat-value">${formatMoney(cf.government_received_this_month)}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Sub payments released</span><span class="perf-stat-value">${formatMoney(cf.sub_payments_released_this_month)}</span></div>
        <div class="perf-stat"><span class="perf-stat-label">Net margin retained</span><span class="perf-stat-value">${formatMoney(cf.net_margin_this_month)}</span></div>
      </div>
      <p class="settings-help">YTD — Gov: ${formatMoney(cf.government_received_ytd)} · Sub: ${formatMoney(cf.sub_payments_released_ytd)} · Net: ${formatMoney(cf.net_margin_ytd)}</p>
    </section>
    <section class="perf-section">
      <h3>Active contract list</h3>
      <table class="pricing-table perf-contract-table">
        <thead><tr><th>Contract</th><th>Status</th><th>Invoice status</th><th>Alerts</th></tr></thead>
        <tbody>${contractRows || '<tr><td colspan="4">No active performance contracts.</td></tr>'}</tbody>
      </table>
    </section>`;
}

function renderPerformanceTabMount(noticeId) {
  return `<div id="performance-tab-mount" class="performance-tab" data-notice-id="${escapeHtml(noticeId)}">
    <p class="settings-help">Loading performance data…</p>
  </div>`;
}

async function loadContractPerformanceTab(noticeId) {
  const mount = document.getElementById("performance-tab-mount");
  if (!mount || mount.dataset.noticeId !== noticeId) return;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/performance`);
    const data = await res.json();
    mount.innerHTML = renderContractPerformance(data);
    bindPerformanceTabEvents(noticeId, data);
  } catch (err) {
    mount.innerHTML = `<p class="perf-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderContractPerformance(data) {
  const p = data.performance || {};
  const inv = data.current_invoice;
  const sub = data.current_sub_payment;
  const hist = data.invoice_history || [];
  const summary = data.invoice_summary || {};
  let banners = "";
  if (data.payment_overdue_alert?.message) {
    banners += `<div class="perf-banner perf-banner-red">${escapeHtml(data.payment_overdue_alert.message)}</div>`;
  }
  if (data.amendment_banner?.message) {
    banners += `<div class="perf-banner perf-banner-red perf-amendment-banner">
      ${escapeHtml(data.amendment_banner.message)}
      <button type="button" class="btn btn-secondary-action btn-sm" data-dismiss-amendments>Amendments reviewed</button>
    </div>`;
    const atts = data.amendment_banner.attachments || [];
    if (atts.length) {
      banners += `<ul class="perf-amendment-list">${atts.map((a) => `<li>${escapeHtml(a.description || a.key || "Document")} — ${escapeHtml(a.posted_date || "")}</li>`).join("")}</ul>`;
    }
  }
  const optionWarnings = (p.option_year_warnings || [])
    .map((w) => `<div class="perf-banner perf-banner-${w.level}">${escapeHtml(w.message)}</div>`)
    .join("");
  const cparsBlock =
    p.status === "completed" || p.cpars_rating || p.cpars_expected_date
      ? `<section class="perf-block">
          <h4>CPARS</h4>
          ${p.cpars_reminder ? `<div class="perf-banner perf-banner-yellow">${escapeHtml(p.cpars_reminder)}</div>` : ""}
          <p class="settings-help">You have 14 days to review and comment on your CPARS rating before it is finalized. Log into cpars.gov immediately when notified.</p>
          <label>Expected evaluation date <input type="date" class="settings-input perf-field" data-perf-field="cpars_expected_date" value="${p.cpars_expected_date || ""}"></label>
          <label>Rating
            <select class="settings-input perf-field" data-perf-field="cpars_rating">
              ${CPARS_RATINGS.map((r) => `<option value="${r}" ${p.cpars_rating === r ? "selected" : ""}>${r}</option>`).join("")}
            </select>
          </label>
          <label>Comments <textarea class="settings-input perf-field" data-perf-field="cpars_comments" rows="2">${escapeHtml(p.cpars_comments || "")}</textarea></label>
        </section>`
      : "";

  return `
    ${banners}
    <section class="perf-block">
      <h4>Contract details</h4>
      <div class="perf-form-grid">
        <label>Status
          <select class="settings-input perf-field" data-perf-field="status">
            ${PERF_STATUSES.map((s) => `<option value="${s.value}" ${p.status === s.value ? "selected" : ""}>${s.label}</option>`).join("")}
            <option value="won" ${p.status === "won" ? "selected" : ""}>Won (legacy)</option>
            <option value="bidding" ${p.status === "bidding" ? "selected" : ""}>Bidding</option>
            <option value="submitted" ${p.status === "submitted" ? "selected" : ""}>Submitted</option>
          </select>
        </label>
        <label>Contract number <input class="settings-input perf-field" data-perf-field="government_contract_number" value="${escapeHtml(p.government_contract_number || "")}"></label>
        <label>Award date <input type="date" class="settings-input perf-field" data-perf-field="award_date" value="${p.award_date || ""}"></label>
        <label>PoP start <input type="date" class="settings-input perf-field" data-perf-field="period_of_performance_start" value="${p.period_of_performance_start || ""}"></label>
        <label>PoP end <input type="date" class="settings-input perf-field" data-perf-field="period_of_performance_end" value="${p.period_of_performance_end || ""}"></label>
        <label>Option years remaining <input type="number" min="0" class="settings-input perf-field" data-perf-field="option_years_remaining" value="${p.option_years_remaining ?? ""}"></label>
        <label>Invoicing system
          <select class="settings-input perf-field" data-perf-field="invoicing_system">
            <option value="">—</option>
            ${INVOICING_SYSTEMS.map((s) => `<option value="${s}" ${p.invoicing_system === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </label>
        <label class="checkbox-inline"><input type="checkbox" class="perf-field" data-perf-field="invoicing_system_confirmed" ${p.invoicing_system_confirmed ? "checked" : ""}> Invoicing system confirmed</label>
      </div>
      <div class="perf-form-grid">
        <label>COR name <input class="settings-input perf-field" data-perf-field="cor_name" value="${escapeHtml(p.cor_name || "")}"></label>
        <label>COR email <input class="settings-input perf-field" data-perf-field="cor_email" value="${escapeHtml(p.cor_email || "")}"></label>
        <label>COR phone <input class="settings-input perf-field" data-perf-field="cor_phone" value="${escapeHtml(p.cor_phone || "")}"></label>
        <label>CO name <input class="settings-input perf-field" data-perf-field="co_name" value="${escapeHtml(p.co_name || "")}"></label>
        <label>CO email <input class="settings-input perf-field" data-perf-field="co_email" value="${escapeHtml(p.co_email || "")}"></label>
        <label>CO phone <input class="settings-input perf-field" data-perf-field="co_phone" value="${escapeHtml(p.co_phone || "")}"></label>
      </div>
      <label class="checkbox-inline perf-stop-work">
        <input type="checkbox" id="perf-stop-work-toggle" ${p.stop_work_issued ? "checked" : ""}> Stop work issued
      </label>
      <label>Stop work date <input type="date" class="settings-input perf-field" data-perf-field="stop_work_issued_date" value="${p.stop_work_issued_date || ""}"></label>
      <button type="button" class="btn btn-secondary-action" id="perf-save-contract">Save contract details</button>
    </section>

    ${p.option_years_remaining > 0 ? `<section class="perf-block">
      <h4>Option year tracker</h4>
      <p>Period ends: <strong>${p.period_of_performance_end || "—"}</strong> · Days remaining: <strong>${p.days_to_period_end ?? "—"}</strong> · Option years left: <strong>${p.option_years_remaining}</strong></p>
      ${optionWarnings}
      <button type="button" class="btn btn-primary" id="perf-exercise-option">Option year exercised</button>
    </section>` : ""}

    <section class="perf-block">
      <h4>Current month invoice</h4>
      ${inv ? renderCurrentInvoice(inv, data.notice_id) : `<p class="settings-help">No invoice for this month yet.</p>
        <button type="button" class="btn btn-secondary-action" id="perf-create-invoice">Create this month's invoice</button>`}
    </section>

    <section class="perf-block">
      <h4>Sub payment status</h4>
      ${sub ? renderCurrentSubPayment(sub, data.notice_id) : `<p class="settings-help">No sub payment record yet.</p>
        <button type="button" class="btn btn-secondary-action" id="perf-create-sub-payment">Record sub invoice</button>`}
    </section>

    <section class="perf-block">
      <h4>Invoice history</h4>
      <p>Avg days to payment: <strong>${summary.avg_days_to_payment ?? "—"}</strong> · Total invoiced: <strong>${formatMoney(summary.total_invoiced)}</strong> · Total received: <strong>${formatMoney(summary.total_received)}</strong></p>
      <table class="pricing-table">
        <thead><tr><th>Period</th><th>Amount</th><th>Submitted</th><th>Paid</th><th>Days</th><th>Status</th></tr></thead>
        <tbody>${hist.map((i) => `<tr>
          <td>${escapeHtml(i.billing_period_start || "")} – ${escapeHtml(i.billing_period_end || "")}</td>
          <td>${formatMoney(i.invoice_amount)}</td>
          <td>${escapeHtml(i.invoice_submitted_date || "—")}</td>
          <td>${escapeHtml(i.payment_received_date || "—")}</td>
          <td>${i.days_to_payment ?? "—"}</td>
          <td><span class="perf-badge ${statusBadgeClass(i.status)}">${escapeHtml(i.status)}</span></td>
        </tr>`).join("") || '<tr><td colspan="6">No invoices yet.</td></tr>'}</tbody>
      </table>
    </section>
    ${cparsBlock}`;
}

function renderCurrentInvoice(inv, noticeId) {
  const overdue = inv.is_overdue ? `<div class="perf-banner perf-banner-red">Invoice overdue — no payment within 45 days of submission.</div>` : "";
  return `
    ${overdue}
    <p><span class="perf-badge ${statusBadgeClass(inv.status)}">${escapeHtml(inv.status)}</span> · ${escapeHtml(inv.invoice_number)} · ${formatMoney(inv.invoice_amount)}</p>
    <p>Period: ${escapeHtml(inv.billing_period_start || "")} – ${escapeHtml(inv.billing_period_end || "")}</p>
    <div class="perf-form-grid" data-invoice-id="${inv.id}">
      <label>Submitted <input type="date" class="settings-input" data-inv-field="invoice_submitted_date" value="${inv.invoice_submitted_date || ""}"></label>
      <label>Method
        <select class="settings-input" data-inv-field="invoice_submission_method">
          ${INVOICING_SYSTEMS.map((s) => `<option value="${s}" ${inv.invoice_submission_method === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
      </label>
      <label>Accepted <input type="date" class="settings-input" data-inv-field="invoice_accepted_date" value="${inv.invoice_accepted_date || ""}"></label>
      ${inv.days_since_submission != null ? `<p class="settings-help">${inv.days_since_submission} days since submission · Expected payment: ${inv.expected_payment_date || "—"}</p>` : ""}
      <label class="checkbox-inline"><input type="checkbox" id="inv-paid-toggle" ${inv.payment_received_date ? "checked" : ""}> Payment received</label>
      <label>Payment date <input type="date" class="settings-input" data-inv-field="payment_received_date" value="${inv.payment_received_date || ""}"></label>
      <label>Payment amount <input type="number" step="0.01" class="settings-input" data-inv-field="payment_amount" value="${inv.payment_amount ?? ""}"></label>
    </div>
    <button type="button" class="btn btn-secondary-action" data-save-invoice>Save invoice</button>
    <button type="button" class="btn btn-secondary-action" data-stop-work-template>Stop work notice template</button>
    <div id="stop-work-template-box" class="perf-template-box" hidden></div>`;
}

function renderCurrentSubPayment(sub, noticeId) {
  const warnings = (sub.warnings || [])
    .map((w) => `<div class="perf-banner perf-banner-${w.includes("COR") ? "yellow" : "red"}">${escapeHtml(w)}</div>`)
    .join("");
  const dueClass = sub.status === "Overdue" ? "perf-due-overdue" : "perf-due-normal";
  return `
    ${warnings}
    <p>Status: <span class="perf-badge ${statusBadgeClass(sub.status)}">${escapeHtml(sub.status)}</span> · ${escapeHtml(sub.sub_company_name || "Sub")}</p>
    <p class="${dueClass}">Sub payment due: <strong>${sub.payment_due_date || "—"}</strong></p>
    <div class="perf-form-grid" data-payment-id="${sub.id}">
      <label>Sub invoice received <input type="date" class="settings-input" data-pay-field="sub_invoice_received_date" value="${sub.sub_invoice_received_date || ""}"></label>
      <label>Sub invoice amount <input type="number" step="0.01" class="settings-input" data-pay-field="sub_invoice_amount" value="${sub.sub_invoice_amount ?? ""}"></label>
      <label class="checkbox-inline"><input type="checkbox" data-pay-field="government_signoff_received" ${sub.government_signoff_received ? "checked" : ""}> Government sign-off received</label>
      <label>Sign-off date <input type="date" class="settings-input" data-pay-field="government_signoff_date" value="${sub.government_signoff_date || ""}"></label>
      <label>Sign-off notes <textarea class="settings-input" data-pay-field="government_signoff_notes" rows="2">${escapeHtml(sub.government_signoff_notes || "")}</textarea></label>
      <label class="checkbox-inline"><input type="checkbox" id="sub-paid-toggle" ${sub.payment_released_date ? "checked" : ""}> Payment released</label>
      <label>Released date <input type="date" class="settings-input" data-pay-field="payment_released_date" value="${sub.payment_released_date || ""}"></label>
      <label>Payment amount <input type="number" step="0.01" class="settings-input" data-pay-field="payment_amount" value="${sub.payment_amount ?? ""}"></label>
      <label>Method
        <select class="settings-input" data-pay-field="payment_method">
          <option value="">—</option>
          ${PAYMENT_METHODS.map((m) => `<option value="${m}" ${sub.payment_method === m ? "selected" : ""}>${m}</option>`).join("")}
        </select>
      </label>
    </div>
    <button type="button" class="btn btn-secondary-action" data-save-sub-payment>Save sub payment</button>
    <button type="button" class="btn btn-secondary-action" data-signoff-template>Sign-off request template</button>
    <div id="signoff-template-box" class="perf-template-box" hidden></div>`;
}

function collectPerfFields(root) {
  const payload = {};
  root.querySelectorAll(".perf-field").forEach((el) => {
    const key = el.dataset.perfField;
    if (!key) return;
    if (el.type === "checkbox") payload[key] = el.checked;
    else if (el.type === "number") payload[key] = el.value === "" ? null : Number(el.value);
    else payload[key] = el.value || null;
  });
  return payload;
}

function bindPerformanceTabEvents(noticeId, data) {
  const root = document.getElementById("performance-tab-mount");
  if (!root) return;

  document.getElementById("perf-save-contract")?.addEventListener("click", async () => {
    const payload = collectPerfFields(root);
    if (document.getElementById("perf-stop-work-toggle")?.checked) {
      payload.stop_work_issued = true;
    } else {
      payload.stop_work_issued = false;
    }
    await patchPerformance(noticeId, payload);
  });

  document.getElementById("perf-stop-work-toggle")?.addEventListener("change", async (e) => {
    await patchPerformance(noticeId, { stop_work_issued: e.target.checked });
    loadContractPerformanceTab(noticeId);
  });

  document.getElementById("perf-exercise-option")?.addEventListener("click", async () => {
    if (!confirm("Advance period of performance by one year and decrement option years?")) return;
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/option-year/exercise`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json();
      alert(err.detail || "Failed");
      return;
    }
    loadContractPerformanceTab(noticeId);
    showSyncStatus("Option year exercised.");
  });

  root.querySelector("[data-dismiss-amendments]")?.addEventListener("click", async () => {
    await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/amendments/dismiss`, { method: "POST" });
    loadContractPerformanceTab(noticeId);
    loadContracts();
  });

  document.getElementById("perf-create-invoice")?.addEventListener("click", async () => {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), 1);
    const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const fmt = (d) => d.toISOString().slice(0, 10);
    await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/invoices`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ billing_period_start: fmt(start), billing_period_end: fmt(end) }),
    });
    loadContractPerformanceTab(noticeId);
  });

  document.getElementById("perf-create-sub-payment")?.addEventListener("click", async () => {
    const invId = data.current_invoice?.id;
    await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/sub-payments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invoice_id: invId || null }),
    });
    loadContractPerformanceTab(noticeId);
  });

  root.querySelector("[data-save-invoice]")?.addEventListener("click", async () => {
    const grid = root.querySelector("[data-invoice-id]");
    const id = grid?.dataset.invoiceId;
    if (!id) return;
    const payload = {};
    grid.querySelectorAll("[data-inv-field]").forEach((el) => {
      payload[el.dataset.invField] = el.value || null;
    });
    await patchInvoice(id, payload);
    loadContractPerformanceTab(noticeId);
  });

  root.querySelector("[data-save-sub-payment]")?.addEventListener("click", async () => {
    const grid = root.querySelector("[data-payment-id]");
    const id = grid?.dataset.paymentId;
    if (!id) return;
    const payload = {};
    grid.querySelectorAll("[data-pay-field]").forEach((el) => {
      if (el.type === "checkbox") payload[el.dataset.payField] = el.checked;
      else payload[el.dataset.payField] = el.value || null;
    });
    await patchSubPayment(id, payload);
    loadContractPerformanceTab(noticeId);
  });

  root.querySelector("[data-stop-work-template]")?.addEventListener("click", async () => {
    const invId = data.current_invoice?.id;
    const q = invId ? `?invoice_id=${invId}` : "";
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/stop-work-notice${q}`);
    const tpl = await res.json();
    const box = document.getElementById("stop-work-template-box");
    if (box) {
      box.hidden = false;
      box.innerHTML = `<label>Copy to clipboard</label><textarea class="settings-input perf-template-text" rows="12" readonly>${escapeHtml(tpl.body || "")}</textarea>
        <button type="button" class="btn btn-secondary-action" data-copy-template>Copy</button>`;
      box.querySelector("[data-copy-template]")?.addEventListener("click", () => copyTemplateText(box.querySelector("textarea")));
    }
  });

  root.querySelector("[data-signoff-template]")?.addEventListener("click", async () => {
    const payId = data.current_sub_payment?.id;
    if (!payId) return;
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/signoff-request/${payId}`);
    const tpl = await res.json();
    const box = document.getElementById("signoff-template-box");
    if (box) {
      box.hidden = false;
      box.innerHTML = `<label>Copy to clipboard</label><textarea class="settings-input perf-template-text" rows="10" readonly>${escapeHtml(tpl.body || "")}</textarea>
        <button type="button" class="btn btn-secondary-action" data-copy-template>Copy</button>`;
      box.querySelector("[data-copy-template]")?.addEventListener("click", () => copyTemplateText(box.querySelector("textarea")));
    }
  });
}

async function patchPerformance(noticeId, payload) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/performance`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Save failed");
  }
  showSyncStatus("Performance details saved.");
  await loadContracts();
}

async function patchInvoice(id, payload) {
  await apiFetch(`/api/invoices/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showSyncStatus("Invoice updated.");
}

async function patchSubPayment(id, payload) {
  await apiFetch(`/api/sub-payments/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showSyncStatus("Sub payment updated.");
}

function copyTemplateText(textarea) {
  if (!textarea) return;
  textarea.select();
  document.execCommand("copy");
  showSyncStatus("Copied to clipboard.");
}

async function savePerformanceSettingsFromPage() {
  const wawf = document.getElementById("settings-wawf-password-date")?.value || null;
  const ipp = document.getElementById("settings-ipp-registered")?.checked || false;
  await apiFetch("/api/settings/performance", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wawf_last_password_change: wawf, ipp_registered: ipp }),
  });
}

function loadPerformanceSettingsIntoPage(data) {
  const perf = data.performance || {};
  const wawfEl = document.getElementById("settings-wawf-password-date");
  const ippEl = document.getElementById("settings-ipp-registered");
  const dueEl = document.getElementById("settings-wawf-next-due");
  const statusEl = document.getElementById("settings-wawf-status");
  if (wawfEl) wawfEl.value = perf.wawf_last_password_change || "";
  if (ippEl) ippEl.checked = !!perf.ipp_registered;
  if (dueEl) dueEl.textContent = perf.wawf_next_due ? `Next change due: ${perf.wawf_next_due}` : "";
  if (statusEl && perf.wawf_status?.message) {
    statusEl.innerHTML = `<div class="perf-banner perf-banner-${perf.wawf_status.level === "red" ? "red" : "yellow"}">${escapeHtml(perf.wawf_status.message)}</div>`;
  } else if (statusEl) {
    statusEl.textContent = "";
  }
}

document.getElementById("tab-performance")?.addEventListener("click", () => {
  if (typeof showView === "function") showView("performance");
});