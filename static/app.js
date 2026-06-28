let config = { naics_codes: [], naics_labels: {}, default_min_days: 30, default_min_score: 1 };
let contracts = [];

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
    const titleBlock = headline
      ? `<h3 class="card-title">${escapeHtml(headline)}</h3>
         <p class="card-official-title">${escapeHtml(c.title)}</p>`
      : `<h3 class="card-title">${escapeHtml(c.title)}</h3>
         <p class="card-pending-note">Plain-English summary being generated…</p>`;
    const naicsLine = c.naics_display || c.naics_code || "";
    return `
    <article class="card card-${tone}" data-id="${c.notice_id}">
      <div class="card-header">${screeningBadge(c)}</div>
      <div class="card-due${due.urgent ? " card-due-urgent" : ""}">
        <span class="card-due-label">Due</span>
        <span class="card-due-date">${escapeHtml(due.main)}</span>
        ${due.sub ? `<span class="card-due-days">${escapeHtml(due.sub)}</span>` : ""}
      </div>
      ${titleBlock}
      <p class="card-meta">${escapeHtml(c.agency || "Unknown agency")}</p>
      <p class="card-meta">${escapeHtml(c.location || "Location unknown")}</p>
      <p class="card-subtype"><strong>Sub type:</strong> ${escapeHtml(subType)}</p>
      <p class="card-meta card-naics"><span class="card-naics-label">${escapeHtml(naicsLine)}</span></p>
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

async function openDetail(noticeId) {
  const res = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}`);
  if (!res.ok) return;
  const c = await res.json();
  const due = formatDue(c);
  const summary = c.plain_english_summary || c.executive_summary || c.analysis?.plain_english_summary || c.analysis?.executive_summary;
  const summaryBlock = summary
    ? `<div class="executive-summary">${formatSummaryHtml(summary)}</div>`
    : `<div class="executive-summary-placeholder">
         Plain-English summary is being generated automatically. Refresh in a minute, or analyze now:
         <button type="button" class="btn btn-primary" id="screen-one-btn" style="margin-top:0.75rem">Analyze this contract now</button>
       </div>`;
  const redFlags = c.red_flags?.length
    ? `<ul>${c.red_flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
    : "<p>None</p>";
  const attachments = c.analysis?.attachments_reviewed;
  const attachmentNote = attachments?.length
    ? `<p class="card-meta">PDFs reviewed: ${attachments.map(escapeHtml).join(", ")}</p>`
    : "";

  document.getElementById("modal-content").innerHTML = `
    ${summaryBlock}
    <div class="modal-badges">${screeningBadge(c)}</div>
    ${summary ? "" : `<h2>${escapeHtml(c.title)}</h2>`}
    <div class="detail-due">${due.main}${due.sub ? ` · ${due.sub}` : ""}</div>
    ${attachmentNote}
    <div class="detail-row"><strong>Quick reason</strong><p>${escapeHtml(c.reason || c.analysis?.reason || "-")}</p></div>
    <div class="detail-row"><strong>Sub type needed</strong><p>${escapeHtml(c.sub_type_needed || "-")}</p></div>
    <div class="detail-row"><strong>Red flags</strong>${redFlags}</div>
    <p class="detail-section-title">Contract details</p>
    <div class="detail-row"><strong>Official title</strong><p>${escapeHtml(c.title)}</p></div>
    <div class="detail-row"><strong>Agency</strong><p>${escapeHtml(c.agency || "-")}</p></div>
    <div class="detail-row"><strong>Location</strong><p>${escapeHtml(c.location || "-")}</p></div>
    <div class="detail-row"><strong>NAICS</strong><p>${escapeHtml(c.naics_display || c.naics_code || "-")}</p></div>
    <div class="detail-row"><strong>Set-aside</strong><p>${escapeHtml(c.set_aside || "-")}</p></div>
    <div class="detail-row"><strong>Status</strong><p>${escapeHtml(c.status)}</p></div>
    ${c.link ? `<a class="detail-link" href="${escapeHtml(c.link)}" target="_blank" rel="noopener">View on SAM.gov</a>` : ""}
  `;
  document.getElementById("modal").hidden = false;

  const screenBtn = document.getElementById("screen-one-btn");
  if (screenBtn) {
    screenBtn.addEventListener("click", async () => {
      screenBtn.disabled = true;
      screenBtn.textContent = "Analyzing...";
      const sres = await apiFetch(`/api/contracts/${encodeURIComponent(noticeId)}/screen`, { method: "POST" });
      const data = await sres.json();
      if (sres.ok) {
        await loadContracts();
        openDetail(noticeId);
      } else {
        screenBtn.textContent = "Analyze this contract";
        screenBtn.disabled = false;
        showSyncStatus(data.detail || "Screening failed", true);
      }
    });
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

function closeModal() {
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
