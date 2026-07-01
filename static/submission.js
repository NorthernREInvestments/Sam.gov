/** Submission checklist, CO questions, deadline display. */

let activeSubmissionNoticeId = null;
let deadlineTicker = null;

const TIMEZONE_NOTE =
  "All federal deadlines are typically Eastern Time unless otherwise stated in the solicitation. Verify the time zone before submitting.";

function renderDeadlineBlock(c, { compact = false } = {}) {
  const pkg = c.submission_package || {};
  const dl = pkg.deadline || {};
  if (!c.due_date && !dl.due_date) {
    return `<div class="deadline-block deadline-unknown">
      <strong>Deadline not confirmed</strong> — check solicitation immediately.
      <p class="deadline-tz-note">${escapeHtml(TIMEZONE_NOTE)}</p>
    </div>`;
  }
  const urgency = dl.urgency || "unknown";
  const alert = dl.alert_24h
    ? `<p class="deadline-alert-24h">WARNING — Submission deadline is in less than 24 hours. Submit immediately.</p>`
    : "";
  const dateStr = c.due_date
    ? new Date(c.due_date + "T12:00:00").toLocaleDateString("en-US", {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : "—";
  return `
    <div class="deadline-block deadline-${urgency}${compact ? " deadline-compact" : ""}" data-deadline-notice="${escapeHtml(c.notice_id)}">
      <div class="deadline-main">
        <span class="deadline-label">Submission deadline</span>
        <span class="deadline-date">${escapeHtml(dateStr)}</span>
        <span class="deadline-countdown" data-countdown-for="${escapeHtml(c.notice_id)}">${escapeHtml(dl.label || "")}</span>
      </div>
      ${alert}
      <p class="deadline-tz-note">${escapeHtml(dl.timezone_note || TIMEZONE_NOTE)}</p>
    </div>`;
}

function submissionPackageBadges(c) {
  const pkg = c.submission_package || {};
  const parts = [];
  if (pkg.pricing_schedule_required) {
    parts.push(`<span class="badge badge-pricing-schedule">PRICING SCHEDULE — REQUIRED</span>`);
  }
  if (pkg.multiple_pricing_encouraged) {
    parts.push(`<span class="badge badge-multi-pricing">Multiple Pricing Options Encouraged</span>`);
  }
  if (pkg.sf1449_required) {
    parts.push(`<span class="badge badge-sf1449">SF-1449 Required</span>`);
  }
  const method = pkg.submission_method || c.submission_method;
  if (method && method !== "Unknown") {
    parts.push(`<span class="badge badge-submission-method">Submit via ${escapeHtml(method)}</span>`);
  } else if (method === "Unknown") {
    parts.push(`<span class="badge badge-submission-unknown">Submission method unknown</span>`);
  }
  return parts.join(" ");
}

function renderSubmissionMethodDetail(c) {
  const pkg = c.submission_package || {};
  const method = pkg.submission_method || c.submission_method || "Unknown";
  const detail = pkg.submission_method_detail || {};
  let methodBody = "";
  if (method === "Email" && (pkg.submission_email || c.submission_email)) {
    const email = pkg.submission_email || c.submission_email;
    methodBody = `
      <p>Submit proposal via email to:</p>
      <button type="button" class="btn btn-secondary-action btn-small sub-copy-email" data-copy="${escapeHtml(email)}">${escapeHtml(email)}</button>`;
  } else if (method === "PIEE") {
    methodBody = `
      <p>${escapeHtml(detail.instructions || "Log in to PIEE and upload your proposal package.")}</p>
      <a class="btn btn-secondary-action btn-small" href="https://piee.eb.mil" target="_blank" rel="noopener">Open PIEE</a>`;
  } else if (method === "SAM.gov") {
    methodBody = `<p>Submit through <a href="https://sam.gov/workspace" target="_blank" rel="noopener">SAM.gov workspace</a>.</p>`;
  } else if (method === "Unknown" || detail.warning) {
    methodBody = `<p class="submission-warning">${escapeHtml(detail.warning || "Submission method not confirmed. Check the solicitation or contact the CO before submitting.")}</p>`;
  }
  return `
    <div class="submission-method-panel">
      <p class="detail-item"><span class="detail-item-label">Submission method</span> <strong>${escapeHtml(method)}</strong></p>
      ${methodBody}
      <label class="toggle-row">
        <input type="checkbox" id="submission-method-confirmed" ${c.submission_method_confirmed ? "checked" : ""}>
        Submission method confirmed
      </label>
      <label class="filter-label">Notes</label>
      <textarea class="settings-input" id="submission-method-notes" rows="2">${escapeHtml(c.submission_method_notes || "")}</textarea>
    </div>`;
}

function renderPricingScheduleDetail(c) {
  if (!c.pricing_schedule_required && !(c.submission_package || {}).pricing_schedule_required) return "";
  const pkg = c.submission_package || {};
  const att = pkg.pricing_schedule_attachment;
  const download = att?.id
    ? `<a class="btn btn-primary-action btn-small" href="/api/contracts/${encodeURIComponent(c.notice_id)}/attachments/${att.id}/download" download>Download pricing schedule</a>`
    : "";
  return `
    <div class="pricing-schedule-banner">
      <span class="badge badge-pricing-schedule">PRICING SCHEDULE — REQUIRED</span>
      <p>${escapeHtml(att?.filename || "Pricing document detected in attachments")}</p>
      <p class="pricing-schedule-warning">${escapeHtml(pkg.pricing_schedule_warning || "")}</p>
      ${download}
    </div>`;
}

function renderCoQuestionsPanel(c) {
  const questions = c.co_questions || [];
  if (!questions.length) {
    return `<p class="detail-note">Run full analysis to generate CO compliance questions.</p>`;
  }
  const qDeadline = c.questions_deadline
    ? `<p class="co-questions-deadline">Questions deadline: ${escapeHtml(c.questions_deadline)}</p>`
    : "";
  return `
    <div class="co-questions-panel" data-notice-id="${escapeHtml(c.notice_id)}">
      <p class="co-questions-note">Email questions to the CO listed in the solicitation before the questions deadline. Only ask questions not clearly answered in the solicitation documents.</p>
      ${qDeadline}
      <ul class="co-questions-list">
        ${questions
          .map(
            (q) => `
          <li class="co-question-item" data-qid="${escapeHtml(q.id)}">
            <textarea class="settings-input co-q-text" rows="2">${escapeHtml(q.text || "")}</textarea>
            <div class="co-q-actions">
              <button type="button" class="btn btn-small btn-secondary-action co-q-copy">Copy</button>
              <label class="toggle-row"><input type="checkbox" class="co-q-asked" ${q.asked ? "checked" : ""}> Asked</label>
              <label class="toggle-row"><input type="checkbox" class="co-q-resolved" ${q.resolved ? "checked" : ""}> Resolved</label>
            </div>
            <textarea class="settings-input co-q-response" rows="2" placeholder="CO response">${escapeHtml(q.response || "")}</textarea>
          </li>`
          )
          .join("")}
      </ul>
      <button type="button" class="btn btn-secondary-action btn-small" data-regen-co="${escapeHtml(c.notice_id)}">Regenerate questions</button>
    </div>`;
}

function bindCoQuestionsPanel(container, noticeId) {
  container.querySelectorAll(".co-question-item").forEach((item) => {
    const qid = item.dataset.qid;
    const save = () => {
      const payload = {
        text: item.querySelector(".co-q-text")?.value,
        asked: item.querySelector(".co-q-asked")?.checked,
        resolved: item.querySelector(".co-q-resolved")?.checked,
        response: item.querySelector(".co-q-response")?.value,
      };
      apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/co-questions/${qid}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(() => {});
    };
    item.querySelectorAll("textarea, input").forEach((el) => {
      el.addEventListener("change", save);
      el.addEventListener("blur", save);
    });
    item.querySelector(".co-q-copy")?.addEventListener("click", () => {
      const text = item.querySelector(".co-q-text")?.value;
      if (text) navigator.clipboard.writeText(text).then(() => showSyncStatus("Question copied."));
    });
  });
  container.querySelector("[data-regen-co]")?.addEventListener("click", async () => {
    await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/co-questions/regenerate`, { method: "POST" });
    const c = await fetchContract(noticeId);
    if (c) {
      const panel = container.querySelector(".co-questions-panel");
      if (panel) {
        panel.outerHTML = renderCoQuestionsPanel(c);
        bindCoQuestionsPanel(container, noticeId);
      }
    }
  });
}

function bindSubmissionMethodPanel(noticeId) {
  const confirmed = document.getElementById("submission-method-confirmed");
  const notes = document.getElementById("submission-method-notes");
  const save = () => {
    apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/submission-meta`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        submission_method_confirmed: confirmed?.checked,
        submission_method_notes: notes?.value,
      }),
    }).catch(() => {});
  };
  confirmed?.addEventListener("change", save);
  notes?.addEventListener("blur", save);
  document.querySelectorAll(".sub-copy-email").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(btn.dataset.copy).then(() => showSyncStatus("Email copied."));
    });
  });
}

async function loadSubmissionChecklistInto(noticeId, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = `<p class="pricing-loading">Loading checklist…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/submission-checklist`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load checklist");
    container.innerHTML = renderSubmissionChecklistPage(data);
    container.querySelector("#submission-checklist-back")?.remove();
    bindSubmissionChecklistPage(container, noticeId);
  } catch (err) {
    container.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function openSubmissionChecklist(noticeId) {
  if (typeof openContractDetail === "function") {
    openContractDetail(noticeId, "proposal");
    return;
  }
  activeSubmissionNoticeId = noticeId;
  loadSubmissionChecklistInto(noticeId, "submission-checklist-content");
}

async function loadSubmissionChecklistPage(noticeId) {
  const container = document.getElementById("submission-checklist-content");
  if (!container) return;
  container.innerHTML = `<p class="pricing-loading">Loading checklist…</p>`;
  try {
    const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/submission-checklist`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load checklist");
    container.innerHTML = renderSubmissionChecklistPage(data);
    bindSubmissionChecklistPage(container, noticeId);
  } catch (err) {
    container.innerHTML = `<p class="pricing-panel-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderSubmissionChecklistPage(data) {
  const cl = data.checklist || {};
  const pkg = data.package || {};
  const pct = cl.completion_pct ?? 0;
  const header = cl.all_complete
    ? `<p class="checklist-ready-msg">${escapeHtml(cl.ready_message || "Checklist complete — ready to submit.")}</p>`
    : `<div class="checklist-progress-wrap"><div class="checklist-progress-bar" style="width:${pct}%"></div></div><p class="checklist-progress-label">${cl.complete_count ?? 0} of ${cl.applicable_count ?? 0} items complete (${pct}%)</p>`;

  const sections = (cl.sections || [])
    .map(
      (sec) => `
    <section class="submission-checklist-section">
      <h3>${escapeHtml(sec.label)}</h3>
      <ul class="submission-checklist-items">
        ${(sec.items || [])
          .map((item) => {
            if (!item.applicable) {
              return `<li class="checklist-item checklist-na"><span>N/A</span> ${escapeHtml(item.label)}</li>`;
            }
            return `
            <li class="checklist-item ${item.status === "done" ? "checklist-done" : ""}" data-key="${escapeHtml(item.key)}">
              <label class="checklist-check"><input type="checkbox" class="cl-checked" ${item.checked ? "checked" : ""}> ${escapeHtml(item.label)}</label>
              ${item.na_allowed ? `<label class="checklist-na-toggle"><input type="checkbox" class="cl-na" ${item.na ? "checked" : ""}> N/A</label>` : ""}
              ${item.has_notes ? `<textarea class="settings-input cl-notes" rows="1" placeholder="Notes">${escapeHtml(item.notes || "")}</textarea>` : ""}
              ${item.na ? `<input class="settings-input cl-na-reason" placeholder="N/A reason" value="${escapeHtml(item.na_reason || "")}">` : ""}
            </li>`;
          })
          .join("")}
      </ul>
    </section>`
    )
    .join("");

  const reps = `
    <section class="submission-checklist-section reps-certs-section">
      <h3>Representations &amp; Certifications</h3>
      <p>${escapeHtml(cl.reps_certs_instructions || pkg.reps_certs_instructions || "")}</p>
      <a class="btn btn-primary-action btn-small" href="https://sam.gov/workspace" target="_blank" rel="noopener">Open SAM.gov Workspace</a>
    </section>`;

  const sf = pkg.sf1449_required
    ? `<section class="submission-checklist-section"><h3>SF-1449</h3><p>${escapeHtml(cl.sf1449_instructions || pkg.sf1449_instructions || "")}</p></section>`
    : "";

  return `
    <div class="submission-checklist-page">
      <button type="button" class="btn btn-secondary-action btn-small" id="submission-checklist-back">← Back</button>
      <h2>Submission checklist — ${escapeHtml(data.contract_title || "")}</h2>
      ${renderDeadlineBlock({ notice_id: data.notice_id, due_date: pkg.deadline?.due_date, submission_package: pkg })}
      ${header}
      ${reps}
      ${sf}
      ${sections}
    </div>`;
}

function bindSubmissionChecklistPage(container, noticeId) {
  container.querySelector("#submission-checklist-back")?.addEventListener("click", () => {
    showView("dashboard");
    if (activeSubmissionNoticeId) openDetail(activeSubmissionNoticeId);
  });
  container.querySelectorAll(".checklist-item[data-key]").forEach((li) => {
    const key = li.dataset.key;
    const save = () => {
      const payload = {
        checked: li.querySelector(".cl-checked")?.checked,
        na: li.querySelector(".cl-na")?.checked,
        na_reason: li.querySelector(".cl-na-reason")?.value,
        notes: li.querySelector(".cl-notes")?.value,
      };
      apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/submission-checklist/${key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(() => loadSubmissionChecklistPage(noticeId)).catch(() => {});
    };
    li.querySelectorAll("input, textarea").forEach((el) => el.addEventListener("change", save));
  });
}

function startDeadlineTicker() {
  if (deadlineTicker) return;
  deadlineTicker = setInterval(() => {
    document.querySelectorAll("[data-countdown-for]").forEach(async (el) => {
      const noticeId = el.dataset.countdownFor;
      const c = contracts.find((x) => x.notice_id === noticeId);
      if (c?.submission_package?.deadline?.label) {
        el.textContent = c.submission_package.deadline.label;
      }
    });
  }, 60000);
}

document.addEventListener("DOMContentLoaded", () => {
  startDeadlineTicker();
});
