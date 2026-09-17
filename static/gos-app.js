/** GovCon OS — dashboard, today, pipeline, global CRM views (GET only). */
(function () {
  // M3-only production: do not hijack hash routing or hide M3 views
  if (document.body && document.body.classList.contains("m3-only-app")) {
    return;
  }
  const views = {
    dashboard: document.getElementById("view-gos-dashboard"),
    today: document.getElementById("view-gos-today"),
    pipeline: document.getElementById("view-gos-pipeline"),
    suppliers: document.getElementById("view-gos-suppliers"),
    contacts: document.getElementById("view-gos-contacts"),
    funding: document.getElementById("view-gos-funding"),
    bids: document.getElementById("view-gos-bids"),
    awards: document.getElementById("view-gos-awards"),
  };
  if (!views.dashboard) return;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function hideAll() {
    Object.values(views).forEach((v) => {
      if (v) v.hidden = true;
    });
    [
      "view-dashboard",
      "view-contract-detail",
      "view-performance",
      "view-settings",
      "view-help",
      "view-product",
      "view-deal-workspace",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    });
  }

  function showView(name) {
    hideAll();
    const v = views[name];
    if (v) v.hidden = false;
    document.querySelectorAll(".gos-nav .tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-gos") === name);
    });
  }

  async function loadDashboard() {
    const el = document.getElementById("gos-dashboard-body");
    if (!el) return;
    el.textContent = "Loading…";
    const data = await fetch("/api/os/dashboard").then((r) => r.json());
    const cards = (data.cards || [])
      .map(
        (c) =>
          `<div class="card" style="cursor:pointer;margin:0.5rem;padding:1rem;" data-href="${esc(c.href)}">` +
          `<div style="font-size:0.75rem;">${esc(c.label)}</div>` +
          `<strong style="font-size:1.5rem;">${esc(String(c.count))}</strong></div>`
      )
      .join("");
    el.innerHTML =
      `<p><strong>${esc(data.headline)}</strong> · ${esc(data.as_of)}</p>` +
      `<div style="display:flex;flex-wrap:wrap;">${cards}</div>` +
      `<p style="font-size:0.8rem;">LIVE_API_REQUESTS: ${data.LIVE_API_REQUESTS ?? 0}</p>`;
    el.querySelectorAll("[data-href]").forEach((card) => {
      card.addEventListener("click", () => {
        location.hash = card.getAttribute("data-href").replace("#", "") || "gos-pipeline";
        route();
      });
    });
  }

  async function loadToday() {
    const el = document.getElementById("gos-today-body");
    if (!el) return;
    el.textContent = "Loading…";
    const data = await fetch("/api/os/today").then((r) => r.json());
    const rows = (data.actions || [])
      .map(
        (a) =>
          `<div class="card" style="margin-bottom:0.75rem;">` +
          `<strong>P${esc(String(a.priority))} · ${esc(a.action)}</strong>` +
          `<p>Opp ${esc(String(a.opportunity_id))} — ${esc(a.opportunity_title || "")}</p>` +
          `<p>Deadline: ${esc(a.deadline || "?")} · Blocker: ${esc(a.blocker || "—")}</p>` +
          `<p><em>${esc(a.why || "")}</em></p>` +
          `<div style="display:flex;gap:0.5rem;flex-wrap:wrap;">` +
          `<button type="button" class="btn btn-primary" data-open="${a.opportunity_id}">${esc(
            a.primary_button || "OPEN DEAL"
          )}</button>` +
          `</div></div>`
      )
      .join("");
    el.innerHTML = rows || "<p>No actions queued.</p>";
    el.querySelectorAll("[data-open]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (window.openDealWorkspace) window.openDealWorkspace(Number(btn.getAttribute("data-open")));
      });
    });
  }

  async function loadPipeline() {
    const el = document.getElementById("gos-pipeline-body");
    if (!el) return;
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const bucket = params.get("bucket");
    el.textContent = "Loading…";
    const url = bucket ? `/api/os/pipeline?bucket=${encodeURIComponent(bucket)}` : "/api/os/pipeline";
    const data = await fetch(url).then((r) => r.json());
    const rows = (data.rows || [])
      .map(
        (r) =>
          `<tr style="cursor:pointer" data-id="${r.opportunity_id}">` +
          `<td>${esc(String(r.opportunity_id))}</td>` +
          `<td>${esc(r.title)}</td>` +
          `<td>${esc(r.agency)}</td>` +
          `<td>${esc(r.product || "")}</td>` +
          `<td>${esc(r.deadline || "")}</td>` +
          `<td>${esc(r.pipeline_bucket || "")}</td>` +
          `<td>${esc(r.next_action || "")}</td></tr>`
      )
      .join("");
    el.innerHTML =
      `<p>Active deals${bucket ? ` · filter: ${esc(bucket)}` : ""} (${data.count})</p>` +
      `<table><thead><tr><th>ID</th><th>Opportunity</th><th>Agency</th><th>Product</th><th>Deadline</th><th>Stuck</th><th>Next</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
    el.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => {
        if (window.openDealWorkspace) window.openDealWorkspace(Number(tr.getAttribute("data-id")));
      });
    });
  }

  async function loadSuppliers() {
    const el = document.getElementById("gos-suppliers-body");
    if (!el) return;
    const data = await fetch("/api/os/suppliers").then((r) => r.json());
    el.innerHTML =
      `<p>${data.count} suppliers</p>` +
      (data.suppliers || [])
        .map(
          (s) =>
            `<div class="card" style="margin-bottom:0.5rem;"><strong>${esc(s.name)}</strong>` +
            `<p>Products: ${esc(s.products || "UNKNOWN")} · Open deals: ${esc((s.open_deals || []).join(", "))}</p>` +
            `<p>Last contact: ${esc(s.last_contact || "—")}</p></div>`
        )
        .join("");
  }

  async function loadContacts() {
    const el = document.getElementById("gos-contacts-body");
    if (!el) return;
    const data = await fetch("/api/os/contacts").then((r) => r.json());
    el.innerHTML =
      `<table><thead><tr><th>Name</th><th>Org</th><th>Type</th><th>Phone</th><th>Email</th></tr></thead><tbody>` +
      (data.contacts || [])
        .map(
          (c) =>
            `<tr><td>${esc(c.name)}</td><td>${esc(c.organization)}</td><td>${esc(c.type)}</td>` +
            `<td>${esc(c.phone)}</td><td>${esc(c.email)}</td></tr>`
        )
        .join("") +
      `</tbody></table>`;
  }

  async function loadFunding() {
    const el = document.getElementById("gos-funding-body");
    if (!el) return;
    const data = await fetch("/api/os/funding").then((r) => r.json());
    const b = data.buckets || {};
    el.innerHTML =
      `<p><strong>${esc(data.headline)}</strong></p>` +
      `<p>Needs research: ${(b.needs_research || []).length} · Blocked: ${(b.blocked || []).length} · Viable: ${(b.viable || []).length}</p>` +
      `<h4>Blocked</h4>` +
      (b.blocked || [])
        .map((d) => `<div class="card">${esc(d.title)} (${d.contract_id})</div>`)
        .join("") +
      `<h4>Provider profiles</h4><p>${(data.providers || []).length} on file</p>`;
  }

  async function loadBids() {
    const el = document.getElementById("gos-bids-body");
    if (!el) return;
    const data = await fetch("/api/os/bids").then((r) => r.json());
    const b = data.buckets || {};
    function rows(key) {
      return (b[key] || [])
        .map(
          (r) =>
            `<tr data-id="${r.contract_id}" style="cursor:pointer"><td>${r.contract_id}</td><td>${esc(
              r.title
            )}</td><td>${esc(r.deadline)}</td><td>${esc(r.bid_ready)}</td><td>${esc(r.next_action)}</td></tr>`
        )
        .join("");
    }
    el.innerHTML =
      `<h4>Bid Ready</h4><table><tbody>${rows("BID_READY")}</tbody></table>` +
      `<h4>Building</h4><table><tbody>${rows("BUILDING")}</tbody></table>` +
      `<h4>Due Soon</h4><table><tbody>${rows("DUE_SOON")}</tbody></table>`;
    el.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => {
        if (window.openDealWorkspace) window.openDealWorkspace(Number(tr.getAttribute("data-id")));
      });
    });
  }

  async function loadAwards() {
    const el = document.getElementById("gos-awards-body");
    if (!el) return;
    const data = await fetch("/api/os/awards").then((r) => r.json());
    el.innerHTML = (data.awards || [])
      .map(
        (a) =>
          `<div class="card"><strong>${esc(a.title)}</strong> · ${esc(a.lifecycle_status)} · ${esc(
            a.agency
          )}</div>`
      )
      .join("");
  }

  const loaders = {
    dashboard: loadDashboard,
    today: loadToday,
    pipeline: loadPipeline,
    suppliers: loadSuppliers,
    contacts: loadContacts,
    funding: loadFunding,
    bids: loadBids,
    awards: loadAwards,
  };

  function route() {
    const h = (location.hash || "").replace("#", "");
    const name = h.split("?")[0].replace("gos-", "") || "dashboard";
    if (loaders[name]) {
      showView(name);
      loaders[name]();
    } else if (h === "gos-dashboard" || h === "dashboard") {
      showView("dashboard");
      loadDashboard();
    } else if (h === "gos-today" || h === "today") {
      showView("today");
      loadToday();
    } else if (h.startsWith("gos-pipeline")) {
      showView("pipeline");
      loadPipeline();
    }
  }

  document.querySelectorAll(".gos-nav .tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const g = btn.getAttribute("data-gos");
      location.hash = g === "dashboard" ? "gos-dashboard" : `gos-${g}`;
      route();
    });
  });

  window.addEventListener("hashchange", route);
  if (/^gos-/.test((location.hash || "").replace("#", ""))) route();
})();
