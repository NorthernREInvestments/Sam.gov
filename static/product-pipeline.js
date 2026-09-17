/** Opportunities + Product Pipeline — GET only on load (no paid AI / no SAM). */
(function () {
  const tab = document.getElementById("tab-product");
  const view = document.getElementById("view-product");
  if (!tab || !view) return;

  function hideOthers() {
    [
      "view-dashboard",
      "view-contract-detail",
      "view-performance",
      "view-settings",
      "view-help",
      "view-gos-dashboard",
      "view-gos-today",
      "view-gos-pipeline",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    });
    document.querySelectorAll(".main-nav .tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    view.hidden = false;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function loadDiscovery() {
    const listEl = document.getElementById("discovery-opp-list");
    const funnelEl = document.getElementById("discovery-funnel-summary");
    const cls = document.getElementById("disc-filter-class")?.value || "";
    const st = document.getElementById("disc-filter-status")?.value || "";
    const jur = document.getElementById("disc-filter-jur")?.value || "";
    const params = new URLSearchParams();
    if (cls) params.set("classification", cls);
    if (st) params.set("status", st);
    if (jur) params.set("jurisdiction", jur);
    try {
      const [funnel, opps] = await Promise.all([
        fetch("/api/discovery/funnel").then((r) => r.json()),
        fetch("/api/discovery/opportunities?" + params.toString()).then((r) => r.json()),
      ]);
      if (funnelEl) {
        const f = funnel.funnel || {};
        funnelEl.textContent =
          "Funnel: raw " +
          (f.RAW_NOTICES ?? 0) +
          " → unique " +
          (f.UNIQUE_OPPORTUNITIES ?? 0) +
          " → core product " +
          (f.CORE_PRODUCT ?? 0) +
          " → research " +
          (f.RESEARCH_CANDIDATES ?? 0) +
          " → active deals " +
          (f.ACTIVE_DEALS ?? 0) +
          " | SAM:0 OpenAI:0";
      }
      if (listEl) {
        const rows = (opps.opportunities || [])
          .map((o) => {
            return (
              `<div class="card" style="margin-bottom:0.75rem;">` +
              `<strong>${escapeHtml(o.title)}</strong>` +
              `<p>${escapeHtml(o.buyer || "")} · ${escapeHtml(o.jurisdiction || "")} · ` +
              `Deadline: ${escapeHtml(o.deadline || "UNKNOWN")}</p>` +
              `<p><span class="badge">${escapeHtml(o.product_classification)}</span> ` +
              `<span class="badge">Priority ${escapeHtml(String(o.research_priority ?? "?"))} ` +
              `(${escapeHtml(o.research_priority_label || "heuristic")})</span> ` +
              `<span class="badge">${escapeHtml(o.source || "")}</span></p>` +
              `<p>${escapeHtml(o.interesting_reason || "")}</p>` +
              (o.obvious_blocker
                ? `<p style="color:#8a1f1f;">Blocker: ${escapeHtml(o.obvious_blocker)}</p>`
                : "") +
              `<div style="display:flex;gap:0.35rem;flex-wrap:wrap;">` +
              `<button type="button" class="btn btn-secondary-action" data-disc-act="review" data-id="${o.id}">REVIEW</button>` +
              `<button type="button" class="btn btn-primary" data-disc-act="start" data-id="${o.id}">START DEAL</button>` +
              `<button type="button" class="btn btn-secondary-action" data-disc-act="watch" data-id="${o.id}">WATCH</button>` +
              `<button type="button" class="btn btn-secondary-action" data-disc-act="reject" data-id="${o.id}">REJECT</button>` +
              (o.contract_id
                ? `<button type="button" class="btn btn-secondary-action" data-open-deal="${o.contract_id}">OPEN DEAL</button>`
                : "") +
              `</div></div>`
            );
          })
          .join("");
        listEl.innerHTML = rows || "<p>No discovered opportunities yet. Use “Load fixture discovery”.</p>";
        listEl.querySelectorAll("[data-disc-act]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const id = Number(btn.getAttribute("data-id"));
            const act = btn.getAttribute("data-disc-act");
            if (act === "start") {
              const res = await fetch(`/api/discovery/opportunities/${id}/start-deal`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
              }).then((r) => r.json());
              if (res.contract_id && window.openDealWorkspace) {
                window.openDealWorkspace(res.contract_id);
              }
              return loadAll();
            }
            const statusMap = { review: "NEEDS_REVIEW", watch: "WATCH", reject: "REJECTED" };
            await fetch(`/api/discovery/opportunities/${id}/status`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ status: statusMap[act] || "NEEDS_REVIEW" }),
            });
            return loadDiscovery();
          });
        });
        listEl.querySelectorAll("[data-open-deal]").forEach((btn) => {
          btn.addEventListener("click", () => {
            if (window.openDealWorkspace) window.openDealWorkspace(Number(btn.getAttribute("data-open-deal")));
          });
        });
      }
    } catch (err) {
      if (listEl) listEl.textContent = "Failed to load discovery (no paid retry).";
    }
  }

  async function loadSources() {
    const el = document.getElementById("discovery-source-registry");
    if (!el) return;
    try {
      const data = await fetch("/api/discovery/sources").then((r) => r.json());
      const s = data.summary || {};
      el.innerHTML =
        `<p>Registry: total ${s.total ?? 0} · implemented ${s.implemented ?? 0} · ` +
        `partial ${s.partial ?? 0} · planned ${s.planned ?? 0} · auth ${s.auth_required ?? 0}</p>` +
        `<table><thead><tr><th>Source</th><th>Type</th><th>Platform</th><th>Status</th><th>Health</th><th>Enabled</th></tr></thead><tbody>` +
        (data.sources || [])
          .slice(0, 80)
          .map(
            (r) =>
              `<tr><td>${escapeHtml(r.source_name)}</td><td>${escapeHtml(r.source_type)}</td>` +
              `<td>${escapeHtml(r.platform_family || "—")}</td>` +
              `<td>${escapeHtml(r.adapter_status)}</td>` +
              `<td>${escapeHtml(r.health)}</td>` +
              `<td>${r.enabled ? "yes" : "no"}</td></tr>`
          )
          .join("") +
        `</tbody></table>` +
        `<p style="font-size:0.8rem;">PLANNED sources are not operational. SAM broad discovery blocked.</p>`;
    } catch (err) {
      el.textContent = "Source registry unavailable.";
    }
  }

  async function loadPipeline() {
    const countsEl = document.getElementById("product-pipeline-counts");
    const listsEl = document.getElementById("product-pipeline-lists");
    const costEl = document.getElementById("product-cost-summary");
    if (countsEl) countsEl.innerHTML = "<p>Loading…</p>";
    try {
      const [pipe, cost] = await Promise.all([
        fetch("/api/product/pipeline").then((r) => r.json()),
        fetch("/api/product/cost-summary").then((r) => r.json()),
      ]);
      if (costEl) {
        const bud = cost.sam_budget || {};
        const usage = cost.api_usage || {};
        costEl.textContent =
          "Paid AI on load: false | SAM on load: false | " +
          "SAM used: " +
          (bud.used ?? usage.sam_used_today ?? "?") +
          " / remaining: " +
          (bud.remaining ?? usage.sam_remaining ?? "?") +
          " (limit " +
          (bud.configured_limit ?? usage.sam_daily_limit ?? "?") +
          ") | Research events: " +
          (cost.research_events_count ?? "?") +
          " | Deal states: " +
          (cost.deal_states_count ?? "?");
      }
      const counts = pipe.counts || {};
      if (countsEl) {
        countsEl.innerHTML = Object.keys(counts)
          .map(
            (k) =>
              `<div class="card card-compact"><strong>${k}</strong><div>${counts[k]}</div></div>`
          )
          .join("");
      }
      const buckets = pipe.buckets || {};
      if (listsEl) {
        listsEl.innerHTML = Object.keys(buckets)
          .map((k) => {
            const items = buckets[k] || [];
            if (!items.length) return "";
            const rows = items
              .slice(0, 25)
              .map(
                (c) =>
                  `<li><a href="#deal-${c.id}" onclick="if(window.openDealWorkspace){openDealWorkspace(${c.id});return false;}">${c.id}: ${escapeHtml(c.title || "")}</a> ` +
                  `<span class="badge">${escapeHtml(c.core_fit || "")}</span> ` +
                  `<span class="badge">${escapeHtml(c.decision || c.pipeline_stage || "")}</span></li>`
              )
              .join("");
            return `<section><h3>${escapeHtml(k)}</h3><ul>${rows}</ul></section>`;
          })
          .join("");
      }
    } catch (err) {
      if (countsEl) countsEl.textContent = "Failed to load product pipeline (no paid retry).";
    }
  }

  async function loadAll() {
    await Promise.all([loadDiscovery(), loadPipeline(), loadSources()]);
  }

  tab.addEventListener("click", () => {
    hideOthers();
    loadAll();
  });
  const refresh = document.getElementById("product-pipeline-refresh");
  if (refresh) refresh.addEventListener("click", loadAll);
  ["disc-filter-class", "disc-filter-status", "disc-filter-jur"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", loadDiscovery);
  });
  const fixBtn = document.getElementById("disc-run-fixtures");
  if (fixBtn) {
    fixBtn.addEventListener("click", async () => {
      fixBtn.disabled = true;
      try {
        await fetch("/api/discovery/run-fixtures", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dry_run: false }),
        });
        await loadAll();
      } finally {
        fixBtn.disabled = false;
      }
    });
  }
})();
