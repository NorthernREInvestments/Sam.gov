/** M3 mobile operator experience — phone/tablet/desktop responsive; backend authoritative. */
(function () {
  const M3_VIEWS = ["home", "opportunities", "actions", "sources", "verify", "deal-room"];
  let cache = { dashboard: null, actions: null, sources: null, deal: null, mode: null, pursuits: null, learning: null };
  let lastDealId = null;
  let activeLearningRecordId = null;

  function esc(s) {
    if (typeof escapeHtml === "function") return escapeHtml(String(s ?? ""));
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtMoney(v) {
    if (v === null || v === undefined || v === "" || v === "UNKNOWN") return "UNKNOWN";
    const n = Number(v);
    if (Number.isNaN(n)) return "UNKNOWN";
    return "$" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function fmtDays(d) {
    if (d === null || d === undefined || d === "UNKNOWN") return "UNKNOWN";
    if (typeof d === "number") return d + " days";
    return String(d);
  }

  function hideLegacyViews() {
    ["view-dashboard", "view-settings", "view-performance", "view-help", "view-contract-detail", "view-product", "view-deal-workspace"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    });
    document.querySelectorAll("[id^='view-gos-']").forEach((el) => {
      el.hidden = true;
    });
  }

  function showM3View(name) {
    hideLegacyViews();
    M3_VIEWS.forEach((v) => {
      const el = document.getElementById("view-m3-" + v);
      if (el) el.hidden = v !== name;
    });
    if (name === "settings") {
      const settings = document.getElementById("view-settings");
      if (settings) settings.hidden = false;
      M3_VIEWS.forEach((v) => {
        const el = document.getElementById("view-m3-" + v);
        if (el) el.hidden = true;
      });
      if (typeof loadSettingsPage === "function") loadSettingsPage();
    }
    document.querySelectorAll(".m3-nav-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.m3View === name || (name === "deal-room" && btn.dataset.m3View === "opportunities"));
    });
    document.body.classList.add("m3-mobile-active");
    if (name === "home") loadHome();
    if (name === "opportunities") loadOpportunities();
    if (name === "actions") loadActions();
    if (name === "sources") loadSources();
    if (name === "verify") loadVerify();
  }

  function oppCard(o) {
    return `<article class="m3-opp-card" data-cid="${esc(o.canonical_id)}" role="button" tabindex="0">
      <div class="m3-opp-buyer">${esc(o.buyer)}</div>
      <h3 class="m3-opp-title">${esc(o.title)}</h3>
      <dl class="m3-kv">
        <div><dt>Deadline</dt><dd>${esc(fmtDays(o.days_remaining))}</dd></div>
        <div><dt>Revenue</dt><dd>${esc(fmtMoney(o.supported_revenue))}</dd></div>
        <div><dt>Supported Profit</dt><dd>${esc(fmtMoney(o.supported_profit))}</dd></div>
        <div><dt>Confidence</dt><dd>${esc(o.profit_confidence)}</dd></div>
        <div><dt>Status</dt><dd>${esc(o.lifecycle)}</dd></div>
        <div><dt>Funding</dt><dd>${esc(o.funding_state)}</dd></div>
      </dl>
      <p class="m3-next"><strong>Next:</strong> ${esc(o.next_action || "—")}</p>
      <p class="m3-why muted">Why waiting: ${esc(o.why_waiting || "UNKNOWN")}</p>
    </article>`;
  }

  function actionCard(a) {
    return `<article class="m3-action-card" data-cid="${esc(a.canonical_id)}" role="button" tabindex="0">
      <div class="m3-priority">${esc(a.priority_label || "NORMAL")}</div>
      <h3 class="m3-opp-title">${esc(a.opportunity_title)}</h3>
      <p class="m3-buyer muted">${esc(a.buyer)}</p>
      <dl class="m3-kv">
        <div><dt>Need</dt><dd>${esc(a.need)}</dd></div>
        <div><dt>Why</dt><dd>${esc(a.reason)}</dd></div>
        <div><dt>Deadline</dt><dd>${esc(a.deadline)}</dd></div>
        <div><dt>Missing</dt><dd>${esc((a.missing_evidence || []).slice(0, 4).join(", ") || "UNKNOWN")}</dd></div>
        <div><dt>Impact</dt><dd>${esc(a.expected_impact)}</dd></div>
      </dl>
    </article>`;
  }

  function pursuitCard(c) {
    return `<article class="m3-opp-card" data-cid="${esc(c.canonical_id)}" role="button" tabindex="0">
      <div class="m3-priority">${c.recommend_for_first_pursuit ? "RECOMMENDED" : "CANDIDATE"}</div>
      <h3 class="m3-opp-title">${esc(c.title || c.canonical_id)}</h3>
      <p class="m3-buyer muted">${esc(c.buyer || "")}</p>
      <dl class="m3-kv">
        <div><dt>Score</dt><dd>${esc(c.score_with_learning != null ? c.score_with_learning : c.score)}</dd></div>
        <div><dt>Base</dt><dd>${esc(c.score)}</dd></div>
        <div><dt>Category</dt><dd>${esc(c.category)}</dd></div>
        <div><dt>Source</dt><dd>${esc(c.source_id)}</dd></div>
      </dl>
      <p class="muted">${esc(c.rationale || "")}</p>
    </article>`;
  }

  async function fetchJson(url, options) {
    const res = await (typeof apiFetch === "function" ? apiFetch(url, options) : fetch(url, options));
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try {
        const j = await res.json();
        detail = j.detail || j.error || detail;
      } catch (_) {}
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  async function postJson(url, body) {
    return fetchJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  async function loadHome(force) {
    try {
      if (!cache.dashboard || force) cache.dashboard = await fetchJson("/api/m3/mobile/dashboard");
      const d = cache.dashboard;
      const actionsEl = document.getElementById("m3-home-actions");
      const oppsEl = document.getElementById("m3-home-opps");
      const empty = document.getElementById("m3-home-empty");
      const topActions = d.top_actions || [];
      const opps = d.active_opportunities || [];
      if (actionsEl) actionsEl.innerHTML = topActions.slice(0, 5).map(actionCard).join("") || "<p class='muted'>No actions</p>";
      if (oppsEl) oppsEl.innerHTML = opps.slice(0, 12).map(oppCard).join("");
      if (empty) empty.hidden = opps.length > 0 || topActions.length > 0;
      wireCardClicks(actionsEl);
      wireCardClicks(oppsEl);
    } catch (e) {
      const oppsEl = document.getElementById("m3-home-opps");
      if (oppsEl) oppsEl.innerHTML = `<p class="m3-empty">Unable to load dashboard.</p>`;
    }
  }

  async function loadOpportunities(force) {
    try {
      if (!cache.dashboard || force) cache.dashboard = await fetchJson("/api/m3/mobile/dashboard");
      const list = document.getElementById("m3-opps-list");
      const opps = cache.dashboard.active_opportunities || [];
      if (list) {
        list.innerHTML = opps.map(oppCard).join("") || "<p class='m3-empty'>No active opportunities.</p>";
        wireCardClicks(list);
      }
    } catch (_) {
      /* non-fatal */
    }
  }

  async function loadActions(force) {
    try {
      if (!cache.actions || force) cache.actions = await fetchJson("/api/m3/mobile/actions");
      const list = document.getElementById("m3-actions-list");
      const empty = document.getElementById("m3-actions-empty");
      const actions = cache.actions.actions || [];
      if (list) {
        list.innerHTML = actions.map(actionCard).join("");
        wireCardClicks(list);
      }
      if (empty) empty.hidden = actions.length > 0;
    } catch (_) {
      /* non-fatal */
    }
  }

  async function loadSources(force) {
    try {
      if (!cache.sources || force) cache.sources = await fetchJson("/api/m3/mobile/sources");
      const s = cache.sources;
      const body = document.getElementById("m3-sources-body");
      if (!body) return;
      const by = s.by_health || {};
      const rows = Object.keys(by)
        .map((k) => `<div class="m3-kv-row"><span>${esc(k)}</span><strong>${esc(by[k])}</strong></div>`)
        .join("");
      body.innerHTML = `<article class="m3-info-card">
        <h3>Source health</h3>
        <p><strong>${esc(s.healthy_production)}</strong> / ${esc(s.registered)} HEALTHY_PRODUCTION</p>
        <div class="m3-kv-list">${rows}</div>
        <p class="muted">${esc(s.note)}</p>
      </article>`;
    } catch (_) {
      /* non-fatal */
    }
  }

  async function loadVerify(force) {
    try {
      cache.mode = await fetchJson("/api/m3/controlled/mode");
      const st = document.getElementById("m3-mode-status");
      if (st) {
        st.textContent =
          (cache.mode.operating_mode || "") +
          " · controlled=" +
          (cache.mode.controlled_verification_active ? "ON" : "OFF") +
          " · outreach=" +
          (cache.mode.outreach_allowed ? "ON" : "OFF");
      }
      cache.pursuits = await fetchJson("/api/m3/controlled/first-pursuits?limit=8");
      const list = document.getElementById("m3-first-pursuits");
      if (list) {
        list.innerHTML = (cache.pursuits.candidates || []).map(pursuitCard).join("") || "<p class='muted'>No candidates</p>";
        wireCardClicks(list);
      }
      const five = await fetchJson("/api/m3/learning/first-five");
      cache.learning = five;
      const learn = document.getElementById("m3-learning-body");
      if (learn) {
        learn.innerHTML = `<article class="m3-info-card">
          <h3>First five learning</h3>
          <p><strong>${esc(five.transactions_recorded)}</strong> recorded · ${esc(five.slots_remaining)} slots remaining</p>
          <p class="muted">${esc((five.learning_feedback || {}).method || "")}</p>
          <p class="muted">Adjustments: ${esc(((five.learning_feedback || {}).adjustments || []).length)}</p>
          <button type="button" class="m3-back-btn" id="m3-open-sources">View sources</button>
        </article>`;
        document.getElementById("m3-open-sources")?.addEventListener("click", () => showM3View("sources"));
      }
    } catch (e) {
      const list = document.getElementById("m3-first-pursuits");
      if (list) list.innerHTML = `<p class="m3-empty">${esc(e.message || "Unable to load verify")}</p>`;
    }
  }

  function sectionCard(title, html) {
    return `<section class="m3-info-card"><h3>${esc(title)}</h3>${html}</section>`;
  }

  function setLearningMsg(msg) {
    const el = document.getElementById("m3-learning-msg");
    if (el) el.textContent = msg || "";
    const rid = document.getElementById("m3-learning-record-id");
    if (rid) rid.textContent = activeLearningRecordId ? "Record: " + activeLearningRecordId : "No active learning record";
  }

  async function openDealRoom(canonicalId) {
    lastDealId = canonicalId;
    showM3View("deal-room");
    const sections = document.getElementById("m3-deal-sections");
    const title = document.getElementById("m3-deal-title");
    const sub = document.getElementById("m3-deal-sub");
    if (sections) sections.innerHTML = "<p class='muted'>Loading deal room…</p>";
    try {
      const deal = await fetchJson("/api/m3/mobile/deal/" + encodeURIComponent(canonicalId));
      cache.deal = deal;
      const o = deal.overview || {};
      if (title) title.textContent = o.title || "Deal Room";
      if (sub) sub.textContent = (o.buyer || "") + " · " + (o.lifecycle || "");
      const econ = deal.economics || {};
      const fund = deal.funding || {};
      const req = deal.requirements || {};
      const fit = deal.product_fit || {};
      const comp = deal.compliance || {};
      const price = deal.pricing || {};
      const act = deal.actions || {};
      const bomHtml =
        (req.bom_lines || [])
          .slice(0, 12)
          .map(
            (li) =>
              `<li><strong>${esc(li.quantity)} ${esc(li.unit)}</strong> — ${esc(li.description)} <span class="muted">(${esc(li.specification)})</span></li>`
          )
          .join("") || "<li class='muted'>UNKNOWN / no BOM yet</li>";
      if (sections) {
        sections.innerHTML = [
          sectionCard(
            "Overview",
            `<dl class="m3-kv">
              <div><dt>Buyer</dt><dd>${esc(o.buyer)}</dd></div>
              <div><dt>Solicitation</dt><dd>${esc(o.solicitation)}</dd></div>
              <div><dt>Source</dt><dd>${esc(o.source)}</dd></div>
              <div><dt>Deadline</dt><dd>${esc(o.deadline)} (${esc(fmtDays(o.days_remaining))})</dd></div>
              <div><dt>Lifecycle</dt><dd>${esc(o.lifecycle)}</dd></div>
            </dl>`
          ),
          sectionCard(
            "Product Fit",
            `<dl class="m3-kv">
              <div><dt>Category</dt><dd>${esc(fit.category)}</dd></div>
              <div><dt>Classification</dt><dd>${esc(fit.classification)}</dd></div>
              <div><dt>Confidence</dt><dd>${esc(fit.confidence)}</dd></div>
            </dl>`
          ),
          sectionCard(
            "Requirements",
            `<p class="muted">Package: ${esc(req.package_access)}</p><ul class="m3-bom">${bomHtml}</ul>
             <p class="muted">Missing: ${esc((req.missing_information || []).join(", ") || "none listed")}</p>`
          ),
          sectionCard(
            "Economics",
            `<dl class="m3-kv">
              <div><dt>Revenue</dt><dd>${esc(fmtMoney(econ.revenue))}</dd></div>
              <div><dt>Acquisition</dt><dd>${esc(fmtMoney(econ.acquisition_evidence))}</dd></div>
              <div><dt>Freight</dt><dd>${esc(fmtMoney(econ.freight))}</dd></div>
              <div><dt>Financing</dt><dd>${esc(fmtMoney(econ.financing))}</dd></div>
              <div><dt>Expected Profit</dt><dd>${esc(fmtMoney(econ.expected_profit))}</dd></div>
              <div><dt>Confidence</dt><dd>${esc(econ.confidence)}</dd></div>
            </dl><p class="muted">${esc(econ.note)}</p>`
          ),
          sectionCard(
            "Funding",
            `<dl class="m3-kv">
              <div><dt>Capital</dt><dd>${esc(fmtMoney(fund.capital_requirement))}</dd></div>
              <div><dt>Status</dt><dd>${esc(fund.funding_status)}</dd></div>
              <div><dt>Verification</dt><dd>${esc(fund.verification_needs)}</dd></div>
            </dl>
            <p class="muted">Unknowns: ${esc((fund.unknowns || []).join(", ") || "none listed")}</p>
            <p class="muted">UNKNOWN financing is not rejection: ${fund.unknown_financing_is_not_rejection ? "yes" : "no"}</p>`
          ),
          sectionCard(
            "Compliance",
            `<p><strong>Blockers:</strong> ${esc((comp.blockers || []).join("; ") || "none")}</p>
             <p><strong>Missing:</strong> ${esc((comp.missing_items || []).join("; ") || "none")}</p>`
          ),
          sectionCard(
            "Pricing",
            `<dl class="m3-kv">
              <div><dt>Bid status</dt><dd>${esc(price.bid_status)}</dd></div>
              <div><dt>Evidence</dt><dd>${esc(price.evidence_level)}</dd></div>
            </dl>`
          ),
          sectionCard(
            "Actions",
            `<p><strong>M3 can do automatically:</strong> ${act.what_m3_can_do ? "yes" : "no"}</p>
             <p><strong>Requires operator:</strong> ${act.what_operator_must_do ? "yes" : "no"}</p>
             <p><strong>Next:</strong> ${esc(act.next_action)}</p>
             <p class="muted">Why not ready: ${esc(act.why_not_ready)}</p>
             <p class="muted">Mode flags — DEV_NO_OUTREACH: ${deal.DEVELOPMENT_NO_OUTREACH ? "ON" : "OFF"}</p>`
          ),
        ].join("");
      }
      setLearningMsg("");
    } catch (_) {
      if (sections) sections.innerHTML = "<p class='m3-empty'>Deal not found or unavailable.</p>";
    }
  }

  function wireCardClicks(container) {
    if (!container) return;
    container.querySelectorAll("[data-cid]").forEach((el) => {
      const go = () => {
        const cid = el.getAttribute("data-cid");
        if (cid) openDealRoom(cid);
      };
      el.addEventListener("click", go);
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go();
        }
      });
    });
  }

  function wireNav() {
    document.querySelectorAll(".m3-nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = btn.dataset.m3View;
        if (v === "settings") showM3View("settings");
        else showM3View(v);
      });
    });
    document.getElementById("m3-deal-back")?.addEventListener("click", () => showM3View("opportunities"));
    document.getElementById("m3-enable-controlled")?.addEventListener("click", async () => {
      try {
        await postJson("/api/m3/controlled/enable", {
          operator_id: "mobile-operator",
          acknowledgment: true,
        });
        await loadVerify(true);
        setLearningMsg("Controlled verification enabled");
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
    document.getElementById("m3-disable-controlled")?.addEventListener("click", async () => {
      await postJson("/api/m3/controlled/disable", { operator_id: "mobile-operator" });
      await loadVerify(true);
    });
    document.getElementById("m3-start-pursuit")?.addEventListener("click", async () => {
      if (!lastDealId) return;
      try {
        const o = cache.deal?.overview || {};
        const rec = await postJson("/api/m3/learning/start", {
          canonical_id: lastDealId,
          source: o.source,
          buyer: o.buyer,
          why_discovered: "operator_selected_from_mobile",
          why_pursued: "controlled_first_pursuit",
        });
        activeLearningRecordId = rec.record_id;
        setLearningMsg("Pursuit record started");
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
    document.getElementById("m3-load-review")?.addEventListener("click", async () => {
      if (!lastDealId) return;
      try {
        const pkg = await fetchJson("/api/m3/controlled/review/" + encodeURIComponent(lastDealId));
        setLearningMsg("Review score: " + (pkg.first_pursuit_score?.score ?? "n/a") + " · recommend=" + !!pkg.first_pursuit_score?.recommend_for_first_pursuit);
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
    document.getElementById("m3-record-supplier")?.addEventListener("click", async () => {
      if (!activeLearningRecordId) {
        setLearningMsg("Start a pursuit record first");
        return;
      }
      try {
        await postJson("/api/m3/learning/supplier-verification", {
          record_id: activeLearningRecordId,
          supplier: document.getElementById("m3-sv-supplier")?.value || "UNKNOWN",
          product: cache.deal?.overview?.title || "UNKNOWN",
          price: document.getElementById("m3-sv-price")?.value || "UNKNOWN",
          authorized_by: "mobile-operator",
        });
        setLearningMsg("Supplier verification recorded");
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
    document.getElementById("m3-record-financing")?.addEventListener("click", async () => {
      if (!activeLearningRecordId) {
        setLearningMsg("Start a pursuit record first");
        return;
      }
      try {
        const state = document.getElementById("m3-fv-state")?.value || "UNKNOWN";
        await postJson("/api/m3/learning/financing-verification", {
          record_id: activeLearningRecordId,
          financing_path: document.getElementById("m3-fv-path")?.value || "UNKNOWN",
          result: state,
          state: state,
          authorized_by: "mobile-operator",
        });
        setLearningMsg("Financing verification recorded (UNKNOWN≠rejection)");
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
    document.getElementById("m3-record-outcome")?.addEventListener("click", async () => {
      if (!activeLearningRecordId) {
        setLearningMsg("Start a pursuit record first");
        return;
      }
      try {
        await postJson("/api/m3/learning/outcome", {
          record_id: activeLearningRecordId,
          status: document.getElementById("m3-outcome-status")?.value || "IN_PROGRESS",
          operator_id: "mobile-operator",
        });
        setLearningMsg("Outcome recorded");
      } catch (e) {
        setLearningMsg(String(e.message || e));
      }
    });
  }

  function boot() {
    wireNav();
    const narrow = window.matchMedia("(max-width: 900px)").matches;
    if (narrow) {
      showM3View("home");
    } else {
      document.body.classList.add("m3-desktop-ready");
      loadHome(false).catch(() => {});
    }
  }

  window.M3Mobile = {
    showM3View,
    openDealRoom,
    loadHome,
    loadVerify,
    refresh: () => {
      cache = { dashboard: null, actions: null, sources: null, deal: null, mode: null, pursuits: null, learning: null };
      return loadHome(true);
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
