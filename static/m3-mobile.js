/** M3 mobile operator experience — phone/tablet/desktop responsive; backend authoritative. */
(function () {
  const M3_VIEWS = [
    "home",
    "opportunities",
    "deep-dive",
    "pipeline",
    "history",
    "actions",
    "sources",
    "verify",
    "settings",
    "advanced",
    "deal-room",
    "micro-lab",
  ];
  const ROLE_KEY = "m3_operator_role";
  const OW_LABELS = {
    DISCOVERED: "Discovered",
    QUALIFIED: "Qualified",
    DEEP_RESEARCH: "Deep Research",
    SUPPLIER_VALIDATION: "Supplier Validation",
    FINANCE_REVIEW: "Financing Review",
    BID_PREPARATION: "Bid Preparation",
    SUBMITTED: "Submitted",
    AWARDED: "Awarded",
    ORDERING: "Ordering",
    SHIPPED: "Shipped",
    DELIVERED: "Delivered",
    ACCEPTED: "Accepted",
    INVOICED: "Invoiced",
    PAID: "Paid",
    HISTORY: "History",
  };
  const PIPELINE_ORDER = [
    "QUALIFIED",
    "DEEP_RESEARCH",
    "SUPPLIER_VALIDATION",
    "FINANCE_REVIEW",
    "BID_PREPARATION",
    "SUBMITTED",
    "AWARDED",
    "ORDERING",
    "DELIVERED",
    "PAID",
    "HISTORY",
  ];
  const INTAKE_STATES = new Set(["DISCOVERED", "QUALIFIED"]);
  const EXECUTION_STATES = new Set([
    "DEEP_RESEARCH",
    "SUPPLIER_VALIDATION",
    "FINANCE_REVIEW",
    "BID_PREPARATION",
  ]);
  const APPROVAL_STATES = new Set(["BID_PREPARATION"]);
  const SUBMITTED_STATES = new Set(["SUBMITTED"]);
  const HISTORY_STATES = new Set(["AWARDED", "ORDERING", "DELIVERED", "PAID", "HISTORY"]);

  let cache = {
    dashboard: null,
    actions: null,
    sources: null,
    deal: null,
    mode: null,
    pursuits: null,
    learning: null,
    profile: null,
    discovery: null,
    research: null,
    evidence: null,
    portfolio: null,
  };
  let lastDealId = null;
  let activeLearningRecordId = null;
  let discoveryPollTimer = null;
  let researchPollTimer = null;
  let portfolioFilter = "intake";

  function getRole() {
    try {
      return localStorage.getItem(ROLE_KEY) === "owner" ? "owner" : "va";
    } catch (_) {
      return "va";
    }
  }

  function setRole(role) {
    const r = role === "owner" ? "owner" : "va";
    try {
      localStorage.setItem(ROLE_KEY, r);
    } catch (_) {}
    paintRoleChrome();
  }

  function paintRoleChrome() {
    const role = getRole();
    const banner = document.getElementById("m3-role-banner");
    if (banner) {
      banner.textContent =
        role === "owner"
          ? "View as Owner — UI preference only (not access control)"
          : "View as Operator — UI preference only (not access control)";
    }
    const status = document.getElementById("m3-role-status");
    if (status) {
      status.textContent =
        role === "owner"
          ? "View mode: Owner (display only — not authentication)"
          : "View mode: Operator (display only — not authentication)";
    }
    document.querySelectorAll(".m3-role-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-role") === role);
    });
  }

  function owLabel(state) {
    if (!state) return "—";
    return OW_LABELS[state] || String(state).replace(/_/g, " ");
  }

  function owState(o) {
    return (
      (o && (o.operator_workflow_state || (o.operator_summary && o.operator_summary.operator_workflow_state))) ||
      ""
    );
  }

  function plainNext(o) {
    return (
      (o && (o.operator_next_action || o.NEXT || o.next_action)) ||
      "Review opportunity"
    );
  }

  function plainBlockers(o) {
    const b = (o && (o.operator_blockers || o.blocking_progress || o.execution_critical_blockers)) || [];
    if (!b.length) return [];
    const MAP = {
      ECONOMICS_UNKNOWN: "Economics incomplete — resolve cost and profit before bid",
      ECONOMICS_INCOMPLETE_OR_UNKNOWN: "Economics incomplete — resolve cost and profit before bid",
      SUPPLIER_UNKNOWN: "Supplier path unresolved — identify and validate a supplier",
      SUPPLIER_NOT_VALIDATED: "Supplier not validated — public listing is not enough",
      QUOTE_NOT_EXECUTABLE: "No executable supplier quote — obtain a formal quote for the exact requirement",
      FINANCING_INCOMPATIBLE_OR_UNKNOWN: "Financing unresolved — confirm zero owner cash and no personal guarantee",
      FUNDING_UNRESOLVED: "Financing unresolved — confirm a funding path before approval",
      SUBMISSION_INCOMPLETE_OR_UNKNOWN: "Submission incomplete — finish required documents and acknowledgements",
      PACKAGING_UNRESOLVED: "Packaging unresolved — confirm packaging method and cost",
      DELIVERY_INFEASIBLE_OR_UNKNOWN: "Delivery unresolved — confirm FOB, destination, and lead time",
      AMENDMENT_UNRESOLVED: "Amendment conflict — confirm the latest authoritative quantity and deadline",
      PRODUCT_IDENTITY_UNCONFIRMED: "Product identity unconfirmed — lock exact part/NSN before quoting",
      QUANTITY_UOM_UNCONFIRMED: "Quantity/UOM unconfirmed — normalize units before supplier pricing",
      SUPPLIER_QUOTE_NEEDED: "Supplier quote needed",
    };
    return b.map((x) => {
      const key = String(x || "").toUpperCase();
      if (MAP[key]) return MAP[key];
      return String(x)
        .replace(/_/g, " ")
        .toLowerCase()
        .replace(/\b\w/g, (c) => c.toUpperCase());
    });
  }

  function humanEvidenceLevel(level) {
    const s = String(level || "").toUpperCase();
    if (!s || s === "UNKNOWN" || s === "LEVEL_4_UNKNOWN" || s === "LEVEL_4") return "Pricing evidence incomplete";
    if (s.includes("PUBLIC")) return "Public market price only (not a supplier quote)";
    if (s.includes("SUPPLIER") || s.includes("FORMAL")) return "Supplier quote evidence";
    return String(level).replace(/_/g, " ");
  }

  function whySelected(o) {
    const reasons = [];
    const blockers = new Set((o && o.operator_blockers) || []);
    const flags = new Set((o && o.risk_flags) || []);
    if (!blockers.has("ECONOMICS_UNKNOWN") && (o.Known_gross_spread != null || o.supported_profit != null)) {
      reasons.push("Margin looks workable");
    }
    if (!blockers.has("SUPPLIER_UNKNOWN") && !flags.has("SUPPLIER_QUOTE_NEEDED")) {
      reasons.push("Supplier options available");
    } else {
      reasons.push("Needs supplier quote");
    }
    const conf = o.confidence_level || o.profit_confidence || "";
    if (String(conf).toUpperCase() === "HIGH" || String(conf).toUpperCase() === "STRONG") {
      reasons.push("Product match confidence high");
    } else if (o.Commercial_verification === "COMMERCIAL_VERIFICATION_WORTHY" || o.Deal_state === "COMMERCIAL_VERIFICATION_WORTHY") {
      reasons.push("Historical purchase signal found");
    } else {
      reasons.push("In intake for qualification");
    }
    if (o.FIRST_TRANSACTION_CANDIDATE) reasons.push("Good first-deal candidate");
    return reasons.slice(0, 4);
  }

  function sumMoney(items, pick) {
    let total = 0;
    let any = false;
    (items || []).forEach((it) => {
      const v = pick(it);
      const n = Number(v);
      if (v != null && v !== "" && v !== "UNKNOWN" && !Number.isNaN(n)) {
        total += n;
        any = true;
      }
    });
    return any ? total : null;
  }

  function esc(s) {
    if (typeof escapeHtml === "function") return escapeHtml(String(s ?? ""));
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return String(iso);
      return d.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (_) {
      return String(iso);
    }
  }

  function fmtElapsed(startedAt) {
    if (!startedAt) return "";
    const t = new Date(startedAt).getTime();
    if (Number.isNaN(t)) return "";
    const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + "m " + String(s).padStart(2, "0") + "s";
  }

  function phaseLabel(phase) {
    const map = {
      PREPARING: "Preparing",
      DISCOVERING: "Discovering sources",
      NORMALIZING: "Normalizing",
      DEDUPLICATING: "Deduplicating",
      CHEAP_SCREENING: "Product screening",
      PIPELINE_UPDATE: "Updating pipeline",
      TRACKED_CHANGE_CHECK: "Tracked change check",
      FINALIZING: "Finalizing",
    };
    return map[phase] || phase || "—";
  }

  function renderDiscoveryStatus(st) {
    const el = document.getElementById("m3-discovery-status");
    if (!el) return;
    st = st || {};
    const run = st.current_run || {};
    const lastOk = st.last_successful_completion || {};
    const lastAttempt = st.last_attempt || {};
    const running = !!st.running;
    const pct = Math.max(0, Math.min(100, Number(st.progress_percent || 0)));
    const status = st.status || "IDLE";
    const disc = st.DISCOVERY || {};
    const handoff = st.PIPELINE_HANDOFF || {};
    const research = st.RESEARCH || {};
    let title = "DISCOVERY";
    let tone = "current";
    if (running) {
      title = "DISCOVERY";
      tone = "running";
    } else if (status === "FAILED") {
      title = "DISCOVERY FAILED";
      tone = "failed";
    } else if (handoff.status === "MISMATCH" || lastAttempt.handoff_status === "MISMATCH") {
      title = "PIPELINE HANDOFF MISMATCH";
      tone = "failed";
    } else if (status === "STALE" || status === "NO_SUCCESSFUL_RUN") {
      title = "DISCOVERY DATA STALE";
      tone = "stale";
    } else if (status === "CURRENT") {
      title = "DISCOVERY";
      tone = "current";
    }
    const srcDone = run.sources_completed || run.sources_attempted || disc.sources_attempted || lastOk.sources_successful;
    const srcTotal = run.sources_total || lastOk.sources_attempted;
    const survivors = running ? run.product_screen_survivors : (disc.product_survivors ?? lastOk.product_screen_survivors);
    const records = running ? run.records_retrieved : (disc.records_fetched ?? lastOk.records_retrieved);
    const unique = running ? run.unique_records : (disc.unique_records ?? lastOk.unique_records);
    const transferred = handoff.transferred ?? lastOk.handoff_transferred;
    const discovered = handoff.discovered ?? survivors;
    const handoffFailed = handoff.failed ?? 0;
    const pipelineDelta = (Number(lastOk.pipeline_new || 0) + Number(lastOk.pipeline_updated || 0));
    const bar = `<div class="m3-disc-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>`;
    let body = "";
    if (running) {
      body = `<p class="m3-disc-line"><strong>Running · ${pct}%</strong> · Elapsed: ${esc(fmtElapsed(st.elapsed_hint_started_at || run.started_at))}</p>
        <p class="m3-disc-line">Phase: ${esc(phaseLabel(st.phase || run.phase))}${srcTotal ? ` · Sources: ${esc(srcDone)} / ${esc(srcTotal)}` : ""}</p>
        <p class="m3-disc-line muted">DISCOVERY · Records: ${esc(records ?? "—")} · Unique: ${esc(unique ?? "—")} · Survivors: ${esc(survivors ?? "—")}</p>
        <p class="m3-disc-line muted">PIPELINE HANDOFF · ${esc(handoff.status || "—")} · transferred ${esc(transferred ?? run.handoff_transferred ?? "—")} / ${esc(discovered ?? "—")}${handoffFailed ? ` · failed ${esc(handoffFailed)}` : ""}${handoff.retrying ? " · retrying" : ""}</p>
        <p class="m3-disc-line muted">RESEARCH · queued ${esc(research.queued ?? run.deep_research_queued ?? "—")}</p>`;
    } else if (status === "FAILED" || handoff.status === "MISMATCH") {
      body = `<p class="m3-disc-line">Last attempt: ${esc(fmtWhen(lastAttempt.completed_at))}</p>
        <p class="m3-disc-line">Last successful: ${esc(fmtWhen(lastOk.completed_at))}</p>
        <p class="m3-disc-line muted">${esc(lastAttempt.error_summary || "Discovery/handoff issue")}</p>
        <p class="m3-disc-line muted">PIPELINE · discovered ${esc(discovered ?? "—")} · stored ${esc(transferred ?? "—")} · failed ${esc(handoffFailed)}</p>`;
    } else if (status === "STALE" || status === "NO_SUCCESSFUL_RUN") {
      body = `<p class="m3-disc-line">Last successful: ${esc(fmtWhen(lastOk.completed_at) || "never")}</p>
        <p class="m3-disc-line muted">Scheduled discovery has not successfully completed within the expected freshness window.</p>
        <p class="m3-disc-line muted">Next run: ${esc(fmtWhen(st.next_scheduled_run))}</p>`;
    } else {
      body = `<p class="m3-disc-line"><strong>Discovery stage current</strong> · last run ${pct}%</p>
        <p class="m3-disc-line">Last successful: ${esc(fmtWhen(lastOk.completed_at))} · Next run: ${esc(fmtWhen(st.next_scheduled_run))}</p>
        <p class="m3-disc-line muted">DISCOVERY · ${esc(survivors ?? records ?? 0)} screened · Unique ${esc(unique ?? "—")}</p>
        <p class="m3-disc-line muted">PIPELINE HANDOFF · ${esc(handoff.status || "COMPLETE")} · stored ${esc(transferred ?? pipelineDelta)} / discovered ${esc(discovered ?? "—")}</p>
        <p class="m3-disc-line muted">RESEARCH · queued ${esc(research.queued ?? lastOk.deep_research_queued ?? 0)}</p>`;
      if (lastOk.sources_failed) {
        body += `<p class="m3-disc-line muted">${esc(lastOk.sources_successful)} / ${esc(lastOk.sources_attempted)} sources successful · ${esc(lastOk.sources_failed)} source warnings</p>`;
      }
      if (st.pending_handoff_resume) {
        body += `<p class="m3-disc-line muted">Pending handoff resume: ${esc(st.pending_handoff_run_id)}</p>`;
      }
    }
    el.className = "m3-discovery-status m3-disc-" + tone;
    el.innerHTML = `<div class="m3-disc-main">
        <div class="m3-disc-head"><span class="m3-disc-title">${esc(title)}</span><span class="m3-disc-pct">${pct}%</span></div>
        ${bar}
        ${body}
      </div>
      <div class="m3-disc-actions">
        <button type="button" class="btn m3-disc-run-now" id="m3-discovery-run-now">RUN NOW</button>
        <p class="m3-disc-msg muted" id="m3-discovery-run-msg" hidden></p>
      </div>`;
    const btn = document.getElementById("m3-discovery-run-now");
    if (btn) {
      btn.addEventListener("click", onRunNow);
      if (running) btn.disabled = false;
    }
  }

  function fmtMoneySpend(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "$0.00";
    return "$" + n.toFixed(2);
  }

  function renderResearchStatus(st) {
    const el = document.getElementById("m3-research-status");
    if (!el) return;
    st = st || {};
    const run = st.current_run || {};
    const disp = st.display || {};
    const lastOk = st.last_successful_completion || {};
    const lastAttempt = st.last_attempt || {};
    const running = !!st.running;
    const stalled = !!st.stalled;
    const pct = Math.max(0, Math.min(100, Number(st.progress_percent || 0)));
    const status = st.status || "IDLE";
    let title = "RESEARCH";
    let tone = "idle";
    if (stalled) {
      title = "RESEARCH STALLED";
      tone = "stalled";
    } else if (running) {
      title = "RESEARCH";
      tone = "running";
    } else if (status === "FAILED") {
      title = "RESEARCH FAILED";
      tone = "failed";
    } else if (status === "BACKLOG") {
      title = "RESEARCH BACKLOG";
      tone = "backlog";
    } else if (status === "CURRENT") {
      title = "RESEARCH";
      tone = "idle";
    }
    const processed = Number(disp.processed != null ? disp.processed : run.processed || 0);
    const total = Number(disp.total_candidates != null ? disp.total_candidates : run.total_candidates || 0);
    const queued = Number(disp.queued != null ? disp.queued : st.backlog || 0);
    const rejected = Number(disp.rejected != null ? disp.rejected : run.rejected || lastOk.rejected || 0);
    const advanced = Number(disp.advanced != null ? disp.advanced : run.advanced || lastOk.advanced || 0);
    const deferred = Number(disp.deferred != null ? disp.deferred : run.deferred || lastOk.deferred || 0);
    const failed = Number(disp.failed != null ? disp.failed : run.failed || lastOk.failed || 0);
    const currentTitle = disp.currently_processing_title || run.currently_processing_title || "";
    const etaLabel = running ? (disp.eta_label || run.eta_label || "Estimating...") : null;
    const spend = disp.actual_external_spend != null ? disp.actual_external_spend : run.actual_external_spend || lastOk.actual_external_spend || 0;
    const bar = `<div class="m3-res-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct}%"></span></div>`;
    let body = "";
    if (stalled) {
      body = `<p class="m3-res-line"><strong>STALLED</strong> · Last heartbeat: ${esc(fmtWhen(disp.heartbeat_at || run.heartbeat_at || run.last_heartbeat_at))}</p>
        <p class="m3-res-line muted">Currently: ${esc(currentTitle || "—")} · Queue will resume after recovery</p>`;
    } else if (running) {
      const frac = total ? `${processed} / ${total}` : `${processed}`;
      body = `<p class="m3-res-line"><strong>Research — ${esc(frac)} — ${pct}%</strong></p>
        <p class="m3-res-line">Currently: ${esc(currentTitle || "Preparing…")}</p>
        <p class="m3-res-line">Elapsed: ${esc(fmtElapsed(st.elapsed_hint_started_at || run.started_at))} · Estimated remaining: ${esc(etaLabel || "Estimating...")}</p>
        <p class="m3-res-line muted">Rejected: ${esc(rejected)} · Advanced: ${esc(advanced)} · Deferred: ${esc(deferred)}${failed ? ` · Failed: ${esc(failed)}` : ""}</p>
        <p class="m3-res-line muted">Queued: ${esc(queued)} · Paid spend: ${esc(fmtMoneySpend(spend))}</p>`;
    } else if (status === "FAILED") {
      body = `<p class="m3-res-line">Last attempt: ${esc(fmtWhen(lastAttempt.completed_at))}</p>
        <p class="m3-res-line muted">${esc(lastAttempt.error_summary || "Research run failed")}</p>
        <p class="m3-res-line muted">Backlog: ${esc(st.backlog || 0)}</p>`;
    } else if (status === "BACKLOG") {
      body = `<p class="m3-res-line"><strong>Queued: ${esc(st.backlog || queued)}</strong> · Waiting for automatic research drain</p>
        <p class="m3-res-line muted">${esc(st.idle_reason || "Research backlog pending")}</p>
        <p class="m3-res-line muted">Next tick: ${esc(fmtWhen(st.next_scheduled_run))}</p>`;
    } else {
      body = `<p class="m3-res-line"><strong>${status === "CURRENT" ? "Idle" : "Idle"}</strong>${total ? ` · Last run ${esc(processed)} / ${esc(total)}` : ""}</p>
        <p class="m3-res-line muted">${esc(st.idle_reason || "No research work in progress")}</p>
        <p class="m3-res-line muted">Rejected: ${esc(rejected)} · Advanced: ${esc(advanced)} · Deferred: ${esc(deferred)} · Spend: ${esc(fmtMoneySpend(spend))}</p>`;
    }
    el.className = "m3-research-status m3-res-" + tone;
    el.innerHTML = `<div class="m3-res-main">
        <div class="m3-res-head"><span class="m3-res-title">${esc(title)}</span><span class="m3-res-pct">${pct}%</span></div>
        ${bar}
        ${body}
      </div>
      <div class="m3-res-actions">
        <button type="button" class="btn m3-res-run-now" id="m3-research-run-now">RUN NOW</button>
        <p class="m3-res-msg muted" id="m3-research-run-msg" hidden></p>
      </div>`;
    const btn = document.getElementById("m3-research-run-now");
    if (btn) {
      btn.addEventListener("click", onResearchRunNow);
      btn.disabled = false;
    }
  }

  async function onResearchRunNow() {
    const msg = document.getElementById("m3-research-run-msg");
    const btn = document.getElementById("m3-research-run-now");
    try {
      if (btn) btn.disabled = true;
      const res = await postJson("/api/m3/research/run", {});
      if (res.already_running) {
        if (msg) {
          msg.hidden = false;
          msg.textContent = res.message || "Research already running.";
        }
        cache.research = res.status || cache.research;
        renderResearchStatus(cache.research);
      } else if (res.accepted === false) {
        if (msg) {
          msg.hidden = false;
          msg.textContent = res.message || res.reason || "Unable to start research.";
        }
        cache.research = res.status || cache.research;
        renderResearchStatus(cache.research);
        if (btn) btn.disabled = false;
      } else {
        if (msg) {
          msg.hidden = false;
          msg.textContent = "Research started.";
        }
        cache.research = res.status || cache.research;
        renderResearchStatus(cache.research);
      }
      scheduleResearchPoll(true);
    } catch (e) {
      if (msg) {
        msg.hidden = false;
        msg.textContent = "Unable to start research.";
      }
      if (btn) btn.disabled = false;
    }
  }

  function scheduleResearchPoll(forceFast) {
    if (researchPollTimer) {
      clearTimeout(researchPollTimer);
      researchPollTimer = null;
    }
    const running = !!(cache.research && cache.research.running);
    const ms = forceFast || running ? 5000 : 45000;
    researchPollTimer = setTimeout(async () => {
      try {
        const st = await fetchJson("/api/m3/research/status");
        cache.research = st;
        renderResearchStatus(st);
        if (st.running) {
          cache.dashboard = null;
          await loadHome(true);
          return;
        }
      } catch (_) {
        /* non-fatal */
      }
      scheduleResearchPoll(false);
    }, ms);
  }

  function renderEvidenceStatus(st) {
    const el = document.getElementById("m3-evidence-status");
    if (!el) return;
    st = st || {};
    const deferred = Number(st.deferred || 0);
    const publicCand = Number(st.public_recovery_candidates || 0);
    const packages = Number(st.packages_recovered || 0);
    const auth = Number(st.auth_registration_blocked || 0);
    const web = Number(st.web_research_pending || 0);
    const unresolved = Number(st.genuinely_unresolved || 0);
    const queue = ((st.source_access_queue || {}).portals || []).slice(0, 3);
    el.className = "m3-evidence-status m3-ev-idle";
    el.innerHTML = `<div class="m3-ev-main">
        <div class="m3-ev-head"><span class="m3-ev-title">EVIDENCE ACCESS</span></div>
        <p class="m3-ev-line"><strong>${esc(deferred)}</strong> deferred · <strong>${esc(Number(st.documents_recovered || 0))}</strong> docs · <strong>${esc(Number(st.boms_recovered || 0))}</strong> BOMs · <strong>${esc(packages)}</strong> packages improved</p>
        <p class="m3-ev-line muted">${esc(auth)} auth blocked · ${esc(Number(st.registration_required || 0))} registration · ${esc(unresolved)} unresolved</p>
        ${queue.length ? `<p class="m3-ev-line muted">Top source access: ${esc(queue.map((p) => p.portal + " (" + p.blocked_opportunities + ")").join(", "))}</p>` : ""}
      </div>
      <div class="m3-ev-actions">
        <button type="button" class="btn m3-ev-run-now" id="m3-evidence-run-now">RECOVER</button>
        <p class="m3-ev-msg muted" id="m3-evidence-run-msg" hidden></p>
      </div>`;
    const btn = document.getElementById("m3-evidence-run-now");
    if (btn) btn.addEventListener("click", onEvidenceRecover);
  }

  async function onEvidenceRecover() {
    const msg = document.getElementById("m3-evidence-run-msg");
    const btn = document.getElementById("m3-evidence-run-now");
    try {
      if (btn) btn.disabled = true;
      const res = await postJson("/api/m3/evidence/acquire", { allow_paid: true });
      if (msg) {
        msg.hidden = false;
        msg.textContent = "Evidence recovery started (" + (res.cleared_for_evidence_pass || 0) + " requeued).";
      }
      cache.dashboard = null;
      await loadHome(true);
      scheduleResearchPoll(true);
    } catch (_) {
      if (msg) {
        msg.hidden = false;
        msg.textContent = "Unable to start evidence recovery.";
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function onRunNow() {
    const msg = document.getElementById("m3-discovery-run-msg");
    const btn = document.getElementById("m3-discovery-run-now");
    try {
      if (btn) btn.disabled = true;
      const res = await postJson("/api/m3/discovery/run", {});
      if (res.already_running) {
        if (msg) {
          msg.hidden = false;
          msg.textContent = res.message || "Discovery already running.";
        }
        cache.discovery = res.status || res.run || cache.discovery;
        renderDiscoveryStatus(cache.discovery);
      } else {
        if (msg) {
          msg.hidden = false;
          msg.textContent = "Discovery started.";
        }
        cache.discovery = res.status || cache.discovery;
        renderDiscoveryStatus(cache.discovery);
      }
      scheduleDiscoveryPoll(true);
    } catch (e) {
      if (msg) {
        msg.hidden = false;
        msg.textContent = "Unable to start discovery.";
      }
      if (btn) btn.disabled = false;
    }
  }

  function scheduleDiscoveryPoll(forceFast) {
    if (discoveryPollTimer) {
      clearTimeout(discoveryPollTimer);
      discoveryPollTimer = null;
    }
    const running = !!(cache.discovery && cache.discovery.running);
    const ms = forceFast || running ? 7000 : 45000;
    discoveryPollTimer = setTimeout(async () => {
      try {
        const st = await fetchJson("/api/m3/discovery/status");
        cache.discovery = st;
        renderDiscoveryStatus(st);
        if (st.running) {
          // refresh counts while a run is active
          cache.dashboard = null;
          await loadHome(true);
          return;
        }
      } catch (_) {
        /* non-fatal */
      }
      scheduleDiscoveryPoll(false);
    }, ms);
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
    const dash = document.getElementById("view-dashboard");
    if (dash) dash.hidden = true;
    M3_VIEWS.forEach((v) => {
      const el = document.getElementById("view-m3-" + v);
      if (el) el.hidden = v !== name;
    });
    const legacySettings = document.getElementById("view-settings");
    if (legacySettings) legacySettings.hidden = true;
    document.querySelectorAll(".m3-nav-btn").forEach((btn) => {
      const isActive =
        btn.dataset.m3View === name ||
        (name === "deal-room" && btn.dataset.m3View === "deep-dive") ||
        (name === "verify" && btn.dataset.m3View === "settings") ||
        (name === "advanced" && btn.dataset.m3View === "settings") ||
        (name === "actions" && btn.dataset.m3View === "settings") ||
        (name === "sources" && btn.dataset.m3View === "settings") ||
        (name === "micro-lab" && btn.dataset.m3View === "settings");
      btn.classList.toggle("active", isActive);
    });
    document.body.classList.add("m3-mobile-active", "m3-only-app");
    try {
      const hash = name === "home" ? "m3-home" : "m3-" + name;
      if (location.hash.replace("#", "") !== hash) {
        history.replaceState(null, "", "#" + hash);
      }
    } catch (_) {
      /* ignore */
    }
    paintRoleChrome();
    if (name === "home") loadHome();
    if (name === "opportunities") loadOpportunities();
    if (name === "deep-dive") loadDeepDiveQueue();
    if (name === "pipeline") loadPipeline();
    if (name === "history") loadHistory();
    if (name === "actions") loadActions();
    if (name === "sources") loadSources();
    if (name === "verify") loadVerify();
    if (name === "settings") loadM3Settings();
    if (name === "advanced") loadAdvanced();
    if (name === "micro-lab" && window.MicroPurchaseLab) window.MicroPurchaseLab.open();
  }

  function isOwnerReady(o) {
    if (!o || typeof o !== "object") return false;
    if (o.ready_for_owner_approval === true) return true;
    const gate = o.owner_approval_gate;
    if (gate && gate.ready_for_owner_approval === true) return true;
    const ec = o.execution_compliance;
    if (ec && ec.owner_approval_gate && ec.owner_approval_gate.ready_for_owner_approval === true) return true;
    return false;
  }

  function oppCard(o) {
    const title = o.Opportunity || o.title || o.deal_name || o.canonical_id;
    const buyer = o.Agency || o.buyer || o.agency || "";
    const product = o.Product_BOM || o.product || o.product_name || title;
    const revenue =
      o.Government_benchmark != null && o.Government_benchmark !== "UNKNOWN"
        ? o.Government_benchmark
        : o.Revenue_basis != null && o.Revenue_basis !== "UNKNOWN"
          ? o.Revenue_basis
          : o.estimated_value != null
            ? o.estimated_value
            : o.supported_revenue;
    const profit =
      o.Known_gross_spread != null && o.Known_gross_spread !== "UNKNOWN"
        ? o.Known_gross_spread
        : o.supported_profit;
    const conf = o.confidence_level || o.profit_confidence || "—";
    const stage = owLabel(owState(o));
    const next = plainNext(o);
    const blockers = plainBlockers(o);
    const why = whySelected(o);
    const whyHtml = why
      .map((r) => `<li class="m3-why-item">${esc(r.startsWith("Needs") ? "○ " + r : "✓ " + r)}</li>`)
      .join("");
    const readyHtml = isOwnerReady(o)
      ? `<p class="m3-ready-banner m3-ready-on">READY FOR OWNER APPROVAL</p>`
      : "";
    return `<article class="m3-opp-card m3-operator-card" data-cid="${esc(o.canonical_id)}" role="button" tabindex="0">
      ${readyHtml}
      <div class="m3-opp-buyer">${esc(buyer)}</div>
      <h3 class="m3-opp-title">${esc(product)}</h3>
      <p class="m3-stage-pill">${esc(stage)}</p>
      <dl class="m3-kv">
        <div><dt>Contract value</dt><dd>${esc(fmtMoney(revenue))}</dd></div>
        <div><dt>Estimated profit</dt><dd>${esc(fmtMoney(profit))}</dd></div>
        <div><dt>Confidence</dt><dd>${esc(conf)}</dd></div>
      </dl>
      <div class="m3-why-block">
        <p class="m3-why-label">Why M3 selected</p>
        <ul class="m3-why-list">${whyHtml}</ul>
      </div>
      ${blockers.length ? `<p class="m3-blockers"><strong>Blockers:</strong> ${esc(blockers.join(" · "))}</p>` : ""}
      <p class="m3-next"><strong>Next action:</strong> ${esc(next)}</p>
      <div class="m3-btn-row m3-card-actions" data-stop="1">
        <button type="button" class="m3-back-btn m3-advance-btn" data-cid="${esc(o.canonical_id)}">Advance</button>
        <button type="button" class="m3-back-btn m3-reject-btn" data-cid="${esc(o.canonical_id)}">Reject</button>
      </div>
    </article>`;
  }

  function portfolioTopCard(c) {
    return oppCard(c);
  }

  async function loadOpportunities(force) {
    try {
      if (!cache.portfolio || force) {
        cache.portfolio = await fetchJson("/api/m3/mobile/opportunities");
      }
      const list = document.getElementById("m3-opps-list");
      const topEl = document.getElementById("m3-opps-top-cards");
      let opps = cache.portfolio.opportunities || cache.portfolio.deals || [];
      if (portfolioFilter === "intake") {
        opps = opps.filter((o) => {
          const st = owState(o);
          return !st || INTAKE_STATES.has(st) || st === "DISCOVERED";
        });
      } else if (portfolioFilter === "ftx") {
        opps = opps.filter((o) => o.FIRST_TRANSACTION_CANDIDATE);
      }
      if (topEl) {
        const tops = (cache.portfolio.top_cards || []).slice(0, 3);
        topEl.innerHTML = tops.length ? `<p class="m3-section-label">Top picks</p>` + tops.map(portfolioTopCard).join("") : "";
        wireCardClicks(topEl);
        wireOppActions(topEl);
      }
      if (list) {
        list.innerHTML = opps.map(oppCard).join("") || "<p class='m3-empty'>No opportunities in this queue.</p>";
        wireCardClicks(list);
        wireOppActions(list);
      }
    } catch (_) {
      try {
        if (!cache.dashboard || force) cache.dashboard = await fetchJson("/api/m3/mobile/dashboard");
        const list = document.getElementById("m3-opps-list");
        const opps = cache.dashboard.active_opportunities || [];
        if (list) {
          list.innerHTML = opps.map(oppCard).join("") || "<p class='m3-empty'>No active opportunities.</p>";
          wireCardClicks(list);
          wireOppActions(list);
        }
      } catch (__) {
        /* non-fatal */
      }
    }
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
    const opts = { credentials: "same-origin", ...(options || {}) };
    const res = await (typeof apiFetch === "function" ? apiFetch(url, opts) : fetch(url, opts));
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
      const health = await fetchJson("/api/m3/health").catch(() => null);
      if (health) {
        const badge = document.getElementById("m3-build-badge");
        const modeBadge = document.getElementById("m3-mode-badge");
        if (badge) badge.textContent = health.build_version || "unknown";
        if (modeBadge) {
          modeBadge.textContent = health.DEVELOPMENT_NO_OUTREACH
            ? "DEV_NO_OUTREACH"
            : health.operating_mode || "MODE";
        }
      }
      cache.dashboard = await fetchJson("/api/m3/mobile/dashboard");
      const d = cache.dashboard;
      const od = d.operator_dashboard || {};
      const activeWork = od.active_work || [];
      const attention = od.attention_needed || {};
      const actions = d.top_actions || [];
      const opps = d.active_opportunities || [];

      // Soft-refresh engine status for Advanced (do not paint on Dashboard)
      cache.discovery = d.discovery || cache.discovery;
      cache.research = d.research || cache.research;
      cache.evidence = d.evidence || cache.evidence;

      // Awaiting approval = owner gate true only (not BID_PREPARATION alone)
      const approvals = activeWork.filter((w) => isOwnerReady(w));
      const submitted = activeWork.filter((w) => SUBMITTED_STATES.has(w.operator_workflow_state));
      const activeDeals = activeWork.filter(
        (w) => !isOwnerReady(w) && !SUBMITTED_STATES.has(w.operator_workflow_state)
      );

      const expectedProfit = sumMoney(opps, (o) =>
        o.Known_gross_spread != null && o.Known_gross_spread !== "UNKNOWN" ? o.Known_gross_spread : o.supported_profit
      );
      const pipelineValue = sumMoney(activeWork, (w) => w.estimated_value);

      const kpiEl = document.getElementById("m3-home-kpis");
      if (kpiEl) {
        kpiEl.innerHTML = `
          <article class="m3-kpi"><span class="m3-kpi-label">Expected profit</span><strong>${esc(fmtMoney(expectedProfit))}</strong></article>
          <article class="m3-kpi"><span class="m3-kpi-label">Pipeline value</span><strong>${esc(fmtMoney(pipelineValue))}</strong></article>
          <article class="m3-kpi"><span class="m3-kpi-label">Active deals</span><strong>${esc(activeWork.length)}</strong></article>
          <article class="m3-kpi"><span class="m3-kpi-label">Awaiting approval</span><strong>${esc(approvals.length)}</strong></article>`;
      }

      const attentionEl = document.getElementById("m3-home-attention");
      if (attentionEl) {
        const cats = Object.keys(attention || {}).filter((k) => (attention[k] || []).length);
        const role = getRole();
        const headline =
          role === "owner"
            ? approvals.length
              ? `${approvals.length} deal(s) waiting for your approval`
              : "No deals waiting for approval"
            : actions.length
              ? `${actions.length} task(s) need operator action`
              : "No urgent operator tasks";
        attentionEl.innerHTML = `<article class="m3-info-card m3-attention-card">
          <h3>${esc(headline)}</h3>
          <p class="muted">${esc((od.counts && od.counts.attention_total) || 0)} blockers across pipeline · outreach blocked</p>
          ${cats.length ? `<p class="muted">Focus: ${esc(cats.map((c) => c.replace(/_/g, " ")).join(" · "))}</p>` : ""}
        </article>`;
      }

      const compactCard = (w) => {
        const row = opps.find((o) => o.canonical_id === w.canonical_id) || w;
        return `<article class="m3-opp-card" data-cid="${esc(w.canonical_id)}" role="button" tabindex="0">
          <h3 class="m3-opp-title">${esc(w.deal_name || row.Opportunity || w.canonical_id)}</h3>
          <p class="m3-stage-pill">${esc(owLabel(w.operator_workflow_state))}</p>
          <p class="m3-next"><strong>Next:</strong> ${esc(w.operator_next_action || "Review")}</p>
          ${(w.operator_blockers || []).length ? `<p class="m3-blockers">${esc(plainBlockers(w).join(" · "))}</p>` : ""}
        </article>`;
      };

      const approvalsEl = document.getElementById("m3-home-approvals");
      const actionsEl = document.getElementById("m3-home-actions");
      const oppsEl = document.getElementById("m3-home-opps");
      const submittedEl = document.getElementById("m3-home-submitted");
      const empty = document.getElementById("m3-home-empty");

      if (approvalsEl) {
        approvalsEl.innerHTML = approvals.length
          ? approvals.map(compactCard).join("")
          : "<p class='muted'>Nothing waiting for owner approval.</p>";
        wireCardClicks(approvalsEl);
      }
      if (actionsEl) {
        const urgent = actions.slice(0, 5);
        actionsEl.innerHTML = urgent.length
          ? urgent.map(actionCard).join("")
          : "<p class='muted'>No urgent blockers.</p>";
        wireCardClicks(actionsEl);
      }
      if (oppsEl) {
        oppsEl.innerHTML = activeDeals.length
          ? activeDeals.slice(0, 12).map(compactCard).join("")
          : "<p class='muted'>No active deals.</p>";
        wireCardClicks(oppsEl);
      }
      if (submittedEl) {
        submittedEl.innerHTML = submitted.length
          ? submitted.map(compactCard).join("")
          : "<p class='muted'>No submitted opportunities.</p>";
        wireCardClicks(submittedEl);
      }
      if (empty) {
        empty.hidden = !!(approvals.length || activeDeals.length || submitted.length || actions.length);
      }
      paintRoleChrome();
    } catch (e) {
      const oppsEl = document.getElementById("m3-home-opps");
      if (oppsEl) oppsEl.innerHTML = `<p class="m3-empty">Unable to load dashboard. ${esc(e && e.message ? e.message : "")}</p>`;
    }
  }

  async function loadAdvanced() {
    try {
      cache.discovery = await fetchJson("/api/m3/discovery/status").catch(() => cache.discovery);
      renderDiscoveryStatus(cache.discovery || {});
      scheduleDiscoveryPoll(!!(cache.discovery && cache.discovery.running));
      cache.research = await fetchJson("/api/m3/research/status").catch(() => cache.research);
      renderResearchStatus(cache.research || {});
      scheduleResearchPoll(!!(cache.research && cache.research.running));
      cache.evidence = await fetchJson("/api/m3/evidence/status").catch(() => cache.evidence);
      renderEvidenceStatus(cache.evidence || {});
    } catch (_) {
      renderDiscoveryStatus({ status: "IDLE", progress_percent: 0 });
      renderResearchStatus({ status: "IDLE", progress_percent: 0 });
    }
  }

  async function loadDeepDiveQueue(force) {
    try {
      if (!cache.portfolio || force) cache.portfolio = await fetchJson("/api/m3/mobile/opportunities");
      if (!cache.dashboard || force) {
        try {
          cache.dashboard = await fetchJson("/api/m3/mobile/dashboard");
        } catch (_) {}
      }
      const list = document.getElementById("m3-deep-dive-list");
      const empty = document.getElementById("m3-deep-dive-empty");
      const od = (cache.dashboard && cache.dashboard.operator_dashboard) || {};
      let rows = od.active_work || [];
      if (!rows.length) {
        rows = (cache.portfolio.opportunities || []).map((o) => ({
          canonical_id: o.canonical_id,
          deal_name: o.Opportunity || o.title,
          operator_workflow_state: owState(o),
          operator_next_action: plainNext(o),
          operator_blockers: o.operator_blockers || [],
        }));
      }
      const exec = rows.filter((r) => EXECUTION_STATES.has(r.operator_workflow_state) || APPROVAL_STATES.has(r.operator_workflow_state));
      if (list) {
        list.innerHTML = exec.length
          ? exec
              .map(
                (w) => `<article class="m3-opp-card" data-cid="${esc(w.canonical_id)}" role="button" tabindex="0">
            <h3 class="m3-opp-title">${esc(w.deal_name || w.canonical_id)}</h3>
            <p class="m3-stage-pill">${esc(owLabel(w.operator_workflow_state))}</p>
            <p class="m3-next"><strong>Your job:</strong> ${esc(w.operator_next_action || "Continue checklist")}</p>
            <p class="muted">Open Deep Dive checklist →</p>
          </article>`
              )
              .join("")
          : "";
        wireCardClicks(list);
      }
      if (empty) empty.hidden = exec.length > 0;
    } catch (_) {
      const empty = document.getElementById("m3-deep-dive-empty");
      if (empty) {
        empty.hidden = false;
        empty.textContent = "Unable to load Deep Dive queue.";
      }
    }
  }

  async function loadPipeline(force) {
    try {
      if (!cache.portfolio || force) cache.portfolio = await fetchJson("/api/m3/mobile/opportunities");
      const body = document.getElementById("m3-pipeline-body");
      const ladder = document.getElementById("m3-pipeline-ladder");
      const opps = cache.portfolio.opportunities || cache.portfolio.deals || [];
      if (ladder) {
        ladder.innerHTML = PIPELINE_ORDER.map((s) => `<span class="m3-pipe-step">${esc(owLabel(s))}</span>`).join(
          '<span class="m3-pipe-arrow">↓</span>'
        );
      }
      const byState = {};
      PIPELINE_ORDER.forEach((s) => {
        byState[s] = [];
      });
      opps.forEach((o) => {
        const st = owState(o) || "QUALIFIED";
        if (!byState[st]) byState[st] = [];
        if (st !== "DISCOVERED") byState[st].push(o);
      });
      if (body) {
        body.innerHTML = PIPELINE_ORDER.map((s) => {
          const items = byState[s] || [];
          if (!items.length) return "";
          return `<section class="m3-mobile-section">
            <h3 class="m3-section-label">${esc(owLabel(s))} · ${items.length}</h3>
            ${items
              .slice(0, 20)
              .map(
                (o) => `<article class="m3-opp-card" data-cid="${esc(o.canonical_id)}" role="button" tabindex="0">
                <h3 class="m3-opp-title">${esc(o.Opportunity || o.title || o.canonical_id)}</h3>
                <p class="muted">${esc(o.Agency || o.buyer || "")}</p>
                <p class="m3-next"><strong>Next:</strong> ${esc(plainNext(o))}</p>
              </article>`
              )
              .join("")}
          </section>`;
        }).join("") || "<p class='m3-empty'>Pipeline is empty.</p>";
        wireCardClicks(body);
      }
    } catch (_) {
      const body = document.getElementById("m3-pipeline-body");
      if (body) body.innerHTML = "<p class='m3-empty'>Unable to load pipeline.</p>";
    }
  }

  async function loadHistory(force) {
    try {
      if (!cache.portfolio || force) cache.portfolio = await fetchJson("/api/m3/mobile/opportunities");
      let learning = null;
      try {
        learning = await fetchJson("/api/m3/learning/records").catch(() => null);
      } catch (_) {}
      const body = document.getElementById("m3-history-body");
      const empty = document.getElementById("m3-history-empty");
      const opps = (cache.portfolio.opportunities || []).filter((o) => HISTORY_STATES.has(owState(o)));
      const records = (learning && (learning.records || learning.items || learning)) || [];
      const recList = Array.isArray(records) ? records : [];
      let html = "";
      if (recList.length) {
        html += `<section class="m3-mobile-section"><h3 class="m3-section-label">Learning outcomes</h3>`;
        html += recList
          .slice(0, 30)
          .map((r) => {
            const status = r.outcome_status || r.status || r.outcome || "—";
            return `<article class="m3-info-card">
              <h3>${esc(r.title || r.deal_name || r.canonical_id || "Record")}</h3>
              <dl class="m3-kv">
                <div><dt>Outcome</dt><dd>${esc(status)}</dd></div>
                <div><dt>Agency</dt><dd>${esc(r.buyer || r.agency || "—")}</dd></div>
                <div><dt>Supplier</dt><dd>${esc(r.supplier || "—")}</dd></div>
                <div><dt>Margin / notes</dt><dd>${esc(r.lesson || r.notes || r.rationale || "—")}</dd></div>
              </dl>
            </article>`;
          })
          .join("");
        html += `</section>`;
      }
      if (opps.length) {
        html += `<section class="m3-mobile-section"><h3 class="m3-section-label">Closed / post-award deals</h3>`;
        html += opps
          .map(
            (o) => {
              const profit = o.Known_gross_spread != null && o.Known_gross_spread !== "UNKNOWN"
                ? fmtMoney(o.Known_gross_spread)
                : o.supported_profit != null && o.supported_profit !== "UNKNOWN"
                  ? fmtMoney(o.supported_profit)
                  : "UNKNOWN";
              const supplier =
                (o.preferred_supplier || o.supplier || (o.supplier_execution_state && o.supplier_execution_state.state) || "UNKNOWN");
              return `<article class="m3-opp-card" data-cid="${esc(o.canonical_id)}" role="button" tabindex="0">
            <h3 class="m3-opp-title">${esc(o.Opportunity || o.title || o.canonical_id)}</h3>
            <p class="m3-stage-pill">${esc(owLabel(owState(o)))}</p>
            <dl class="m3-kv">
              <div><dt>Buyer</dt><dd>${esc(o.Agency || o.buyer || o.agency || "UNKNOWN")}</dd></div>
              <div><dt>Supplier</dt><dd>${esc(supplier)}</dd></div>
              <div><dt>Profit</dt><dd>${esc(profit)}</dd></div>
              <div><dt>Outcome notes</dt><dd>${esc(o.lesson || o.loss_reason || o.why_waiting || "UNKNOWN")}</dd></div>
            </dl>
          </article>`;
            }
          )
          .join("");
        html += `</section>`;
      }
      if (body) {
        body.innerHTML = html || "";
        wireCardClicks(body);
      }
      if (empty) empty.hidden = !!(html && html.length);
    } catch (_) {
      const empty = document.getElementById("m3-history-empty");
      if (empty) {
        empty.hidden = false;
        empty.textContent = "Unable to load history.";
      }
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
      let coverageHtml = "";
      try {
        const cov = await fetchJson("/api/m3/coverage");
        const tot = cov.TOTAL_SOURCES || {};
        const disc = cov.DISCOVERY || {};
        const pipe = cov.PIPELINE || {};
        const top = ((cov.SOURCE_RECOVERY || {}).Top_10 || []).slice(0, 8);
        const ops = (cov.operator_action_queue || []).slice(0, 5);
        coverageHtml = `<article class="m3-info-card">
          <h3>Coverage</h3>
          <dl class="m3-kv">
            <div><dt>Eligible</dt><dd>${esc(tot.Eligible ?? "—")}</dd></div>
            <div><dt>Productive</dt><dd>${esc(tot.Productive ?? "—")}</dd></div>
            <div><dt>Blocked</dt><dd>${esc(tot.Blocked ?? "—")}</dd></div>
            <div><dt>Needs operator</dt><dd>${esc(tot.Needs_operator ?? "—")}</dd></div>
          </dl>
          <p class="muted">Discovery raw ${esc(disc.Raw ?? "—")} · unique ${esc(disc.Unique ?? "—")} · survivors ${esc(disc.Product_survivors ?? "—")}</p>
          <p class="muted">Pipeline discovered ${esc(pipe.Discovered ?? "—")} · stored ${esc(pipe.Stored ?? "—")} · failed ${esc(pipe.Failed ?? 0)}</p>
        </article>
        <article class="m3-info-card">
          <h3>Top source recovery</h3>
          ${top.map((t) => `<p class="m3-ev-line"><strong>${esc(t.source_name || t.source)}</strong> · ${esc(t.portal_family)} · score ${esc(t.SOURCE_RECOVERY_SCORE)} · ${esc(t.recovery_bucket)} · ~${esc(t.estimated_opportunities_unlocked)} opps</p>`).join("") || "<p class='muted'>No recovery targets</p>"}
        </article>
        <article class="m3-info-card">
          <h3>Operator action queue</h3>
          ${ops.map((o) => `<p class="m3-ev-line"><strong>${esc(o.SOURCE)}</strong> · ${esc(o.STATUS)}<br/><span class="muted">${esc(o.ACTION)} — ${esc(o.WHY_IT_MATTERS)}</span></p>`).join("") || "<p class='muted'>No operator credential actions queued</p>"}
        </article>`;
      } catch (_) {
        coverageHtml = "";
      }
      body.innerHTML = coverageHtml + `<article class="m3-info-card">
        <h3>Source health</h3>
        <p><strong>${esc(s.healthy_production)}</strong> / ${esc(s.registered)} HEALTHY_PRODUCTION · productive ${esc(s.productive_discovery_sources)}</p>
        <div class="m3-kv-list">${rows}</div>
        <p class="muted">${esc(s.note)}</p>
      </article>
      <article class="m3-info-card">
        <h3>Yield / family</h3>
        ${(s.yield_top || []).slice(0, 10).map((y) => `<p class="m3-ev-line"><strong>${esc(y.source)}</strong> · ${esc(y.family)} · ${esc(y.current_records)} records · ${esc(y.health)}</p>`).join("") || "<p class='muted'>Yield populates after discovery cycles</p>"}
      </article>
      <article class="m3-info-card">
        <h3>Discovery gaps</h3>
        ${(s.discovery_gaps || []).slice(0, 8).map((g) => `<p class="m3-ev-line"><strong>${esc(g.gap_type)}</strong> · ${esc(g.target)} · yield ${esc(g.expected_yield)}<br/><span class="muted">${esc(g.detail || "")}</span></p>`).join("") || "<p class='muted'>No ranked gaps</p>"}
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

  async function loadM3Settings(force) {
    try {
      if (!cache.profile || force) cache.profile = await fetchJson("/api/m3/procurement-profile");
      const body = document.getElementById("m3-settings-body");
      if (!body) return;
      const p = cache.profile.profile || {};
      const iso = cache.profile.isolation || {};
      const disc = p.discovery || {};
      const types = (p.target_product_types || []).slice(0, 12).map((t) => `<li>${esc(t)}</li>`).join("");
      const proc = (p.procurement_types || []).map((t) => `<li>${esc(t)}</li>`).join("");
      let credHtml = "<p class='muted'>No portal credentials stored.</p>";
      try {
        const creds = await fetchJson("/api/m3/credentials");
        const portals = Object.values(creds.portals || {});
        if (portals.length) {
          credHtml = portals
            .map(
              (c) =>
                `<li><strong>${esc(c.portal)}</strong> — ${esc(c.username || "—")} · ${c.password_set ? "password set" : "no password"} · ${esc(c.account_status || "")}</li>`
            )
            .join("");
          credHtml = `<ul class="m3-bom">${credHtml}</ul>`;
        }
      } catch (_) {
        /* non-fatal */
      }
      body.innerHTML = `
        <article class="m3-info-card">
          <h3>Procurement profile</h3>
          <p><strong>${esc(p.primary_purpose || "Government product-resale")}</strong></p>
          <p class="muted">Isolated from legacy acquisition: ${p.isolated_from_legacy_acquisition ? "yes" : "no"}</p>
          <p class="muted">NAICS as primary discovery filter: ${disc.naics_is_primary_filter ? "yes" : "no"}</p>
          <p class="muted">Isolation check: ${iso.ok ? "PASS" : "FAIL"}</p>
        </article>
        <article class="m3-info-card">
          <h3>Portal credentials</h3>
          ${credHtml}
          <form id="m3-cred-form" class="m3-cred-form">
            <label>Portal / source<input name="portal" required placeholder="state_ia"></label>
            <label>Login URL<input name="login_url" placeholder="https://..."></label>
            <label>Username / email<input name="username" autocomplete="username"></label>
            <label>Password<input name="password" type="password" autocomplete="current-password"></label>
            <label class="m3-check"><input name="mfa" type="checkbox"> MFA / manual browser login required</label>
            <button type="submit" class="btn">Save credential</button>
            <p class="muted" id="m3-cred-msg"></p>
          </form>
          <p class="muted">No CAPTCHA bypass. No autonomous registration. Stronger encryption can be added later without rewriting source logic.</p>
        </article>
        <article class="m3-info-card">
          <h3>Target product types</h3>
          <ul class="m3-bom">${types || "<li class='muted'>UNKNOWN</li>"}</ul>
        </article>
        <article class="m3-info-card">
          <h3>Procurement types</h3>
          <ul class="m3-bom">${proc}</ul>
          <p class="muted">Primary signals: ${esc((disc.primary_signals || []).join(", "))}</p>
        </article>
        <article class="m3-info-card">
          <h3>What M3 does not use</h3>
          <p class="muted">Legacy facilities-service NAICS (561720 janitorial, 238220 HVAC maintenance, etc.), subcontracting screening scores, owner/operator acquisition criteria.</p>
        </article>`;
      const form = document.getElementById("m3-cred-form");
      if (form) {
        form.addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const fd = new FormData(form);
          const msg = document.getElementById("m3-cred-msg");
          try {
            await postJson("/api/m3/credentials", {
              portal: fd.get("portal"),
              login_url: fd.get("login_url"),
              username: fd.get("username"),
              password: fd.get("password"),
              mfa_or_manual_login_required: !!fd.get("mfa"),
            });
            if (msg) msg.textContent = "Credential saved.";
            await loadM3Settings(true);
          } catch (_) {
            if (msg) msg.textContent = "Unable to save credential.";
          }
        });
      }
    } catch (e) {
      const body = document.getElementById("m3-settings-body");
      if (body) body.innerHTML = `<p class="m3-empty">${esc(e.message || "Unable to load M3 profile")}</p>`;
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

  function checkStatus(ok, blocked) {
    if (blocked) return "Blocked";
    if (ok) return "Complete";
    return "Missing";
  }

  function checklistSection(title, status, items) {
    const cls =
      status === "Complete"
        ? "m3-check-status-complete"
        : status === "Blocked"
          ? "m3-check-status-blocked"
          : "m3-check-status-missing";
    const rows = (items || [])
      .map((it) => {
        const mark = it.done ? "✓" : it.blocked ? "✕" : "○";
        return `<li><span class="m3-check-mark">${mark}</span> ${esc(it.label)}${it.detail ? ` <span class="muted">— ${esc(it.detail)}</span>` : ""}</li>`;
      })
      .join("");
    return `<article class="m3-info-card m3-checklist-section">
      <div class="m3-check-head">
        <h3>${esc(title)}</h3>
        <span class="m3-check-status ${cls}">${esc(status)}</span>
      </div>
      <ul class="m3-check-list">${rows}</ul>
    </article>`;
  }

  function renderDealChecklist(deal) {
    const el = document.getElementById("m3-deal-checklist");
    if (!el) return;
    const req = deal.requirements || {};
    const fit = deal.product_fit || {};
    const comp = deal.compliance || {};
    const price = deal.pricing || {};
    const fund = deal.funding || {};
    const si = deal.supplier_intelligence || {};
    const ci = deal.commercial_intelligence || {};
    const pkgInt = deal.procurement_package || {};
    const econ = deal.economics || {};
    const blockers = new Set(deal.operator_blockers || []);
    const bom = req.bom_lines || pkgInt.BOM || [];
    const suppliers = si.Possible_Suppliers || [];
    const priceItems = ((si.Pricing_Evidence || {}).items || []);
    const hasQuote = priceItems.some((p) => String(p.level || "").includes("QUOTE") || String(p.Source || "").toLowerCase().includes("quote"));

    const productItems = [
      { label: "Exact product identity", done: !!(fit.identity || fit.matched_product || ci.Known_Manufacturer), detail: fit.identity || ci.Known_Manufacturer || "" },
      { label: "Manufacturer / model", done: !!(ci.Known_Manufacturer || fit.manufacturer), detail: ci.Known_Manufacturer || "" },
      { label: "Quantity", done: !!(bom.length || req.quantity), detail: bom.length ? bom.length + " line(s)" : "" },
      { label: "Specifications", done: !!(req.specifications || (bom[0] && bom[0].specification)), detail: "" },
      { label: "Substitutions reviewed", done: !!(fit.substitutions_ok || fit.substitution_policy), detail: "" },
    ];
    const productOk = productItems.filter((i) => i.done).length >= 3;
    const productBlocked = blockers.has("PRODUCT_UNKNOWN");

    const supplierItems = [
      { label: "Supplier contacted", done: suppliers.length > 0, detail: suppliers.length ? suppliers.length + " option(s)" : "" },
      { label: "Quote requested", done: suppliers.length > 0 || hasQuote, detail: "" },
      { label: "Quote received", done: hasQuote, detail: hasQuote ? "Quote evidence present" : "", blocked: blockers.has("SUPPLIER_UNKNOWN") },
      { label: "Availability", done: !!(si.Availability || (suppliers[0] && suppliers[0].availability)), detail: "" },
      { label: "Lead time", done: !!(si.Lead_Time || (suppliers[0] && suppliers[0].lead_time)), detail: "" },
      { label: "Supplier terms", done: !!(si.Terms || (suppliers[0] && suppliers[0].terms)), detail: "" },
    ];
    const supplierOk = hasQuote || (suppliers.length > 0 && !blockers.has("SUPPLIER_UNKNOWN"));
    const supplierBlocked = blockers.has("SUPPLIER_UNKNOWN");

    const pricingItems = [
      { label: "Government historical pricing", done: !!(ci.Government_Value || econ.expected_revenue || price.government_benchmark), detail: "" },
      { label: "Current market pricing", done: priceItems.length > 0 || !!(ci.Estimated_Acquisition), detail: "" },
      { label: "Supplier cost", done: hasQuote || !!(econ.acquisition_cost), detail: "" },
      { label: "Expected margin", done: econ.expected_profit != null && econ.expected_profit !== "UNKNOWN", detail: fmtMoney(econ.expected_profit) },
      { label: "Confidence", done: !!(deal.confidence_level || price.evidence_level), detail: deal.confidence_level || price.evidence_level || "" },
    ];
    const pricingOk = pricingItems.filter((i) => i.done).length >= 3 && !blockers.has("ECONOMICS_UNKNOWN");
    const pricingBlocked = blockers.has("ECONOMICS_UNKNOWN");

    const complianceItems = [
      { label: "Domestic requirements", done: !!(comp.domestic || comp.buy_american || comp.TAA), detail: "" },
      { label: "Brand restrictions", done: comp.brand_restricted != null || !!(comp.brand_notes), detail: "" },
      { label: "Certifications", done: !!(comp.certifications || []).length, detail: "" },
      { label: "Set-asides", done: !!(comp.set_aside || req.set_aside), detail: comp.set_aside || req.set_aside || "" },
      { label: "Solicitation requirements", done: !!(req.special_requirements || comp.solicitation_notes || bom.length), detail: "" },
    ];
    const complianceOk = complianceItems.filter((i) => i.done).length >= 2;

    const financeItems = [
      { label: "Purchase amount", done: !!(fund.amount || econ.capital_required || econ.expected_revenue), detail: fmtMoney(fund.amount || econ.capital_required) },
      { label: "Payment timeline", done: !!(fund.payment_timeline || fund.terms), detail: "" },
      { label: "Funding requirement", done: !!(fund.funding_state || fund.required), detail: fund.funding_state || "" },
      { label: "Financing path", done: !!(fund.path || fund.financing_path), detail: fund.path || fund.financing_path || "", blocked: blockers.has("FUNDING_UNRESOLVED") },
    ];
    const financeOk = !blockers.has("FUNDING_UNRESOLVED") && financeItems.filter((i) => i.done).length >= 2;
    const financeBlocked = blockers.has("FUNDING_UNRESOLVED");

    const bidItems = [
      { label: "Documents complete", done: !!(pkgInt.Documents || pkgInt.documents_complete || deal.evidence), detail: "" },
      { label: "Pricing complete", done: pricingOk, detail: "" },
      { label: "Submission ready", done: APPROVAL_STATES.has(deal.operator_workflow_state) || !!(deal.actions && !deal.actions.why_not_ready), detail: "" },
    ];
    const bidOk = bidItems.filter((i) => i.done).length >= 2;

    el.innerHTML = [
      checklistSection("Product Validation", checkStatus(productOk, productBlocked), productItems),
      checklistSection("Supplier Validation", checkStatus(supplierOk, supplierBlocked), supplierItems),
      checklistSection("Pricing Validation", checkStatus(pricingOk, pricingBlocked), pricingItems),
      checklistSection("Compliance", checkStatus(complianceOk, false), complianceItems),
      checklistSection("Financing", checkStatus(financeOk, financeBlocked), financeItems),
      checklistSection("Bid Preparation", checkStatus(bidOk, false), bidItems),
      `<article class="m3-info-card">
        <h3>Who acts next</h3>
        <p><strong>${esc(isOwnerReady(deal) ? "Owner" : "VA / Operator")}</strong></p>
        <p class="m3-next">${esc(deal.operator_next_action || (deal.actions && deal.actions.next_action) || "Continue checklist")}</p>
        ${(deal.operator_blockers || []).length ? `<p class="m3-blockers">Blockers: ${esc(plainBlockers(deal).join(" · "))}</p>` : ""}
      </article>`,
    ].join("");
  }

  async function openDealRoom(canonicalId) {
    lastDealId = canonicalId;
    showM3View("deal-room");
    const sections = document.getElementById("m3-deal-sections");
    const checklistEl = document.getElementById("m3-deal-checklist");
    const title = document.getElementById("m3-deal-title");
    const sub = document.getElementById("m3-deal-sub");
    const readyBanner = document.getElementById("m3-deal-ready-banner");
    if (sections) sections.innerHTML = "<p class='muted'>Loading technical details…</p>";
    if (checklistEl) checklistEl.innerHTML = "<p class='muted'>Loading checklist…</p>";
    try {
      const deal = await fetchJson("/api/m3/mobile/deal/" + encodeURIComponent(canonicalId));
      cache.deal = deal;
      const o = deal.overview || {};
      const ow = deal.operator_workflow_state || (deal.operator_summary && deal.operator_summary.operator_workflow_state) || "";
      if (title) title.textContent = o.title || "Deep Dive";
      if (sub) {
        sub.textContent =
          (o.buyer || "") + " · " + owLabel(ow) + " · Next: " + (deal.operator_next_action || o.next_action || "Continue checklist");
      }
      renderDealChecklist(deal);
      // Prefer Phase D structured section statuses when present (no UI redesign)
      const ec = deal.execution_compliance || {};
      const dd = ec.deep_dive_sections || {};
      if (checklistEl && dd && Object.keys(dd).length) {
        const mapStatus = (sec) => {
          const code = (sec && sec.code) || "";
          if (code === "COMPLETE") return "Complete";
          if (code === "BLOCKED") return "Blocked";
          if (code === "MISSING" || code === "DEFERRED_TO_ECONOMICS") return "Missing";
          return "Action Required";
        };
        const sectionsOrder = [
          ["Product Validation", "Product"],
          ["Supplier Validation", "Supplier"],
          ["Pricing Validation", "Pricing"],
          ["Compliance", "Compliance"],
          ["Financing", "Financing"],
          ["Bid Preparation", "Bid"],
        ];
        const cards = sectionsOrder.map(([title, key]) => {
          const sec = dd[key] || {};
          const st = mapStatus(sec);
          const cls =
            st === "Complete"
              ? "m3-check-status-complete"
              : st === "Blocked"
                ? "m3-check-status-blocked"
                : "m3-check-status-missing";
          const actions = (sec.actions || []).slice(0, 6);
          const rows = actions.length
            ? actions.map((a) => `<li><span class="m3-check-mark">○</span> ${esc(a)}</li>`).join("")
            : `<li class="muted">${esc(sec.status || "No items")}</li>`;
          return `<article class="m3-info-card m3-checklist-section">
            <div class="m3-check-head"><h3>${esc(title)}</h3><span class="m3-check-status ${cls}">${esc(st)}</span></div>
            <ul class="m3-check-list">${rows}</ul>
          </article>`;
        });
        const gate = ec.owner_approval_gate || {};
        cards.push(`<article class="m3-info-card">
          <h3>Who acts next</h3>
          <p><strong>${esc(gate.ready_for_owner_approval ? "Owner" : "VA / Operator")}</strong></p>
          <p class="m3-next">${esc((gate.va_next_actions && gate.va_next_actions[0]) || deal.operator_next_action || "Continue checklist")}</p>
          ${(gate.reasons || []).length ? `<p class="m3-blockers">${esc(gate.reasons.slice(0, 4).join(" · "))}</p>` : ""}
        </article>`);
        checklistEl.innerHTML = cards.join("");
      }
      if (readyBanner) {
        // Phase E.1 — sole authority: owner_approval_gate.ready_for_owner_approval === true
        // BID_PREPARATION / checklist allPass / legacy READY must never override a blocked gate.
        const gateReady =
          (ec.owner_approval_gate && ec.owner_approval_gate.ready_for_owner_approval === true) ||
          deal.ready_for_owner_approval === true;
        readyBanner.hidden = !gateReady;
        if (gateReady) {
          readyBanner.textContent = "READY FOR OWNER APPROVAL";
          readyBanner.className = "m3-ready-banner m3-ready-on";
        } else {
          readyBanner.textContent = "";
          readyBanner.className = "m3-ready-banner";
        }
      }
      const econ = deal.economics || {};
      const fund = deal.funding || {};
      const req = deal.requirements || {};
      const fit = deal.product_fit || {};
      const comp = deal.compliance || {};
      const price = deal.pricing || {};
      const act = deal.actions || {};
      const ev = deal.evidence || {};
      const commercial = deal.commercial_research || {};
      const ci = deal.commercial_intelligence || {};
      const si = deal.supplier_intelligence || {};
      const compInt = deal.competitive_intelligence || {};
      const execInt = deal.execution_intelligence || {};
      const dealEcon = deal.deal_economics || {};
      const prodInt = deal.product_intelligence || {};
      const pkgInt = deal.procurement_package || {};
      const evRec = deal.evidence_recovery || {};
      const evChain = deal.evidence_chain || {};
      const srcAccess = deal.source_access || {};
      const bomHtml =
        (req.bom_lines || [])
          .slice(0, 12)
          .map(
            (li) =>
              `<li><strong>${esc(li.quantity)} ${esc(li.unit)}</strong> — ${esc(li.description)} <span class="muted">(${esc(li.specification)})</span></li>`
          )
          .join("") || "<li class='muted'>UNKNOWN / no BOM yet</li>";
      const pkgBomHtml =
        ((pkgInt.BOM || [])
          .slice(0, 12)
          .map(
            (li) =>
              `<li><strong>#${esc(li.Line_number)}</strong> ${esc(li.config_type)} · ${esc(li.Quantity)} ${esc(li.Unit)} — ${esc(li.Description)} <span class="muted">${esc(li.Part_number)} · ${esc(li.Confidence)}</span></li>`
          )
          .join("")) || "<li class='muted'>No procurement BOM yet</li>";
      const winnersHtml = (ci.Historical_Winners || [])
        .map((w) => esc(w))
        .join(", ") || "UNKNOWN";
      const acq = ci.Estimated_Acquisition || {};
      const suppliersHtml =
        (si.Possible_Suppliers || [])
          .slice(0, 6)
          .map(
            (s) =>
              `<li>${esc(s.company || "UNKNOWN")} <span class="muted">(${esc(s.role || "")})</span></li>`
          )
          .join("") || "<li class='muted'>No mapped channels yet</li>";
      const priceItems = ((si.Pricing_Evidence || {}).items || [])
        .slice(0, 3)
        .map(
          (p) =>
            `<li>${esc(p.level)} · ${esc(p.amount)} ${esc(p.unit || "USD")} · ${esc(p.Source)} · match ${esc(p.Product_match_confidence)}</li>`
        )
        .join("") || `<li class="muted">${esc(humanEvidenceLevel((si.Pricing_Evidence || {}).level))}</li>`;
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
            "Commercial Intelligence",
            `<dl class="m3-kv">
              <div><dt>Government Value</dt><dd>${esc(ci.Government_Value ?? "UNKNOWN")}</dd></div>
              <div><dt>Historical Winners</dt><dd>${winnersHtml}</dd></div>
              <div><dt>Known Manufacturer</dt><dd>${esc(ci.Known_Manufacturer ?? "UNKNOWN")}</dd></div>
              <div><dt>Supply Confidence</dt><dd>${esc(ci.Supply_Confidence ?? "UNKNOWN")}</dd></div>
              <div><dt>Estimated Acquisition</dt><dd>${esc(acq.low ?? "UNKNOWN")} – ${esc(acq.high ?? "UNKNOWN")} <span class="muted">(${esc(acq.level || acq.status || "")})</span></dd></div>
              <div><dt>Margin Potential</dt><dd>${esc(ci.Margin_Potential ?? "UNKNOWN")}</dd></div>
              <div><dt>Commercial Confidence</dt><dd><strong>${esc(ci.Commercial_Confidence ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Financing Difficulty</dt><dd>${esc(ci.Financing_Difficulty ?? "UNKNOWN")}</dd></div>
              <div><dt>Reseller Fit</dt><dd>${esc(ci.RESELLER_FIT_SCORE ?? "—")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(ci.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">Government price ≠ profit · research only · no supplier contact</p>`
          ),
          sectionCard(
            "Supplier Intelligence",
            `<dl class="m3-kv">
              <div><dt>Product</dt><dd>${esc(si.Product ?? "UNKNOWN")}</dd></div>
              <div><dt>Manufacturer</dt><dd>${esc(si.Manufacturer ?? "UNKNOWN")}</dd></div>
              <div><dt>Cost Confidence</dt><dd><strong>${esc(si.Cost_Confidence ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Margin Status</dt><dd>${esc(si.Margin_Status ?? "MARGIN_PENDING_SUPPLIER_VERIFICATION")}</dd></div>
              <div><dt>First Deal Fit</dt><dd>${esc(si.First_Deal_Fit ?? "UNKNOWN")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(si.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">Possible suppliers</p>
            <ul class="m3-bom">${suppliersHtml}</ul>
            <p class="muted">Pricing evidence</p>
            <ul class="m3-bom">${priceItems}</ul>
            <p class="muted">Research only · no supplier outreach · no invented margins</p>`
          ),
          sectionCard(
            "Competitive Intelligence",
            `<dl class="m3-kv">
              <div><dt>Competition</dt><dd>${esc(compInt.Competition ?? "UNKNOWN")}</dd></div>
              <div><dt>Historical bidders</dt><dd>${esc(compInt.Historical_bidders ?? "UNKNOWN")}</dd></div>
              <div><dt>Winner concentration</dt><dd>${esc(compInt.Winner_concentration ?? "UNKNOWN")}</dd></div>
              <div><dt>Incumbent risk</dt><dd>${esc(compInt.Incumbent_risk ?? "UNKNOWN")} <span class="muted">(${esc(compInt.Incumbent_market || "")})</span></dd></div>
              <div><dt>New entrant advantage</dt><dd>${esc(compInt.New_entrant_advantage ?? "UNKNOWN")}</dd></div>
              <div><dt>First deal score</dt><dd><strong>${esc(compInt.First_deal_score ?? "UNKNOWN")}</strong> · ${esc(compInt.First_deal_priority ?? "")}</dd></div>
              <div><dt>Bucket</dt><dd>${esc(compInt.BUCKET ?? "UNKNOWN")}</dd></div>
              <div><dt>Financeability</dt><dd>${esc(compInt.Financeability ?? "UNKNOWN")}</dd></div>
              <div><dt>Why</dt><dd>${esc(((compInt.Why || []).slice(0, 5).join("; ")) || "—")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(compInt.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">Competition ≠ auto-reject · bidder count alone never rejects</p>`
          ),
          sectionCard(
            "Execution Intelligence",
            `<dl class="m3-kv">
              <div><dt>Contract Value</dt><dd>${esc(execInt.Contract_Value ?? "UNKNOWN")}</dd></div>
              <div><dt>Estimated Capital Needed</dt><dd>${esc(execInt.Estimated_Capital_Needed ?? "UNKNOWN")} <span class="muted">(${esc(execInt.Capital_Need_Band || "")})</span></dd></div>
              <div><dt>Financing Fit</dt><dd>${esc(execInt.Financing_Fit ?? "UNKNOWN")}</dd></div>
              <div><dt>Supplier Risk</dt><dd>${esc(execInt.Supplier_Risk ?? "UNKNOWN")}</dd></div>
              <div><dt>Delivery Risk</dt><dd>${esc(execInt.Delivery_Risk ?? "UNKNOWN")}</dd></div>
              <div><dt>Execution Score</dt><dd><strong>${esc(execInt.Execution_Score ?? "UNKNOWN")}</strong> · ${esc(execInt.Execution_Band ?? "")}</dd></div>
              <div><dt>Action Priority</dt><dd>${esc(execInt.ACTION_PRIORITY_SCORE ?? "—")}</dd></div>
              <div><dt>Bucket</dt><dd>${esc(execInt.BUCKET ?? "UNKNOWN")}</dd></div>
              <div><dt>Recommended Path</dt><dd>${esc(execInt.Recommended_Path ?? "—")}</dd></div>
              <div><dt>Why</dt><dd>${esc(((execInt.Why || []).slice(0, 5).join("; ")) || "—")}</dd></div>
            </dl>
            <p class="muted">Research only · no bids · no financing applications · financing not assumed</p>`
          ),
          sectionCard(
            "Deal Economics",
            `<dl class="m3-kv">
              <div><dt>Contract Value</dt><dd>${esc(dealEcon.Contract_Value ?? "UNKNOWN")}</dd></div>
              <div><dt>Target Profit</dt><dd>${esc(dealEcon.Target_Profit ?? "UNKNOWN")}</dd></div>
              <div><dt>Required Acquisition Cost</dt><dd>${esc(dealEcon.Required_Acquisition_Cost ?? "UNKNOWN")}</dd></div>
              <div><dt>Current Pricing Evidence</dt><dd>${esc(((dealEcon.Current_Pricing_Evidence || {}).cost) ?? "UNKNOWN")} <span class="muted">(${esc(((dealEcon.Current_Pricing_Evidence || {}).level) || "")})</span></dd></div>
              <div><dt>Projected Profit</dt><dd>${esc(dealEcon.Projected_Profit ?? "UNKNOWN")}</dd></div>
              <div><dt>Profit Target Status</dt><dd><strong>${esc(dealEcon.Profit_Target_Status ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Pricing Confidence</dt><dd>${esc(dealEcon.Pricing_Confidence ?? "UNKNOWN")}</dd></div>
              <div><dt>Gap / Improvement</dt><dd>${esc(((dealEcon.Target_Acquisition_Gap || {}).difference) ?? "UNKNOWN")} · ${esc(((dealEcon.Target_Acquisition_Gap || {}).required_improvement_pct) ?? "UNKNOWN")}%</dd></div>
              <div><dt>Economics Priority</dt><dd>${esc(dealEcon.ECONOMICS_PRIORITY_SCORE ?? "—")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(dealEcon.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">Unknown cost ≠ reject · no invented margins · operator notes never hide economics</p>`
          ),
          sectionCard(
            "Product Intelligence",
            `<dl class="m3-kv">
              <div><dt>Product identity</dt><dd>${esc(prodInt.Product_identity ?? "UNKNOWN")}</dd></div>
              <div><dt>Identity confidence</dt><dd><strong>${esc(prodInt.Identity_confidence ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Manufacturer</dt><dd>${esc(prodInt.Manufacturer ?? "UNKNOWN")}</dd></div>
              <div><dt>Part number</dt><dd>${esc(prodInt.Part_number ?? "UNKNOWN")}</dd></div>
              <div><dt>NSN</dt><dd>${esc(prodInt.NSN ?? "UNKNOWN")}</dd></div>
              <div><dt>Quantity</dt><dd>${esc(prodInt.Quantity ?? "UNKNOWN")}</dd></div>
              <div><dt>Contract value</dt><dd>${esc(prodInt.Contract_value ?? "UNKNOWN")} <span class="muted">(${esc(prodInt.Contract_value_confidence || "")})</span></dd></div>
              <div><dt>Pricing evidence</dt><dd>${esc(humanEvidenceLevel((prodInt.Pricing_evidence || {}).level))} · ${esc(((prodInt.Pricing_evidence || {}).items || []).length)} item(s)</dd></div>
              <div><dt>Research readiness</dt><dd>${esc(prodInt.Research_readiness ?? "UNKNOWN")}</dd></div>
              <div><dt>Missing information</dt><dd>${esc(((prodInt.Missing_information || []).join(", ")) || "none listed")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(prodInt.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">${esc(prodInt.Economics_message || "Pricing research only after identity confidence is known")}</p>`
          ),
          sectionCard(
            "Complete Procurement Package",
            `<dl class="m3-kv">
              <div><dt>Solicitation</dt><dd>${esc(((pkgInt.Contract_information || {}).Solicitation_ID) ?? "UNKNOWN")}</dd></div>
              <div><dt>Agency / Buyer</dt><dd>${esc(((pkgInt.Contract_information || {}).Agency) ?? "UNKNOWN")} / ${esc(((pkgInt.Contract_information || {}).Buyer) ?? "UNKNOWN")}</dd></div>
              <div><dt>Contract value</dt><dd>${esc(((pkgInt.Contract_information || {}).Estimated_value) ?? "UNKNOWN")} <span class="muted">(${esc(((pkgInt.Contract_information || {}).Confidence) || "")})</span></dd></div>
              <div><dt>Required products</dt><dd>${esc(((pkgInt.Required_products || []).join("; ")) || "UNKNOWN")}</dd></div>
              <div><dt>Manufacturer</dt><dd>${esc(pkgInt.Manufacturer ?? "UNKNOWN")}</dd></div>
              <div><dt>Exact model</dt><dd>${esc(pkgInt.Exact_models ?? "UNKNOWN")}</dd></div>
              <div><dt>Part number / NSN</dt><dd>${esc(pkgInt.Part_numbers ?? "UNKNOWN")} / ${esc(pkgInt.NSN ?? "UNKNOWN")}</dd></div>
              <div><dt>Quantity</dt><dd>${esc(pkgInt.Quantity ?? "UNKNOWN")}</dd></div>
              <div><dt>Accessories</dt><dd>${esc(((pkgInt.Accessories || []).join("; ")) || "NONE_EVIDENCED")}</dd></div>
              <div><dt>Configuration</dt><dd>${esc(pkgInt.Configuration_completeness ?? "INCOMPLETE")}</dd></div>
              <div><dt>BOM completeness</dt><dd>${esc(pkgInt.BOM_completeness_pct ?? "UNKNOWN")}%</dd></div>
              <div><dt>Pricing readiness</dt><dd>${esc(pkgInt.Pricing_readiness ?? "UNKNOWN")}</dd></div>
              <div><dt>Economics readiness</dt><dd><strong>${esc(pkgInt.Economics_readiness ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Research queue</dt><dd>${esc(pkgInt.Research_queue ?? "UNKNOWN")} · score ${esc(pkgInt.Research_score ?? "—")}</dd></div>
              <div><dt>Missing information</dt><dd>${esc(((pkgInt.Missing_information || []).join(", ")) || "none listed")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(pkgInt.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">Procurement BOM</p>
            <ul class="m3-bom">${pkgBomHtml}</ul>
            <p class="muted">${esc(pkgInt.Economics_message || "Do not invent accessories, compatibility, or prices")}</p>`
          ),
          sectionCard(
            "Evidence Recovery",
            `<dl class="m3-kv">
              <div><dt>Documents found</dt><dd>${esc(((evRec.Documents_found || []).length) || 0)}</dd></div>
              <div><dt>Documents missing</dt><dd>${esc(((evRec.Documents_missing || []).join(", ")) || "none listed")}</dd></div>
              <div><dt>Recovered value</dt><dd>${esc(((evRec.Recovered_values || {}).primary) ?? "UNKNOWN")} <span class="muted">(${esc(((evRec.Recovered_values || {}).confidence) || "")})</span></dd></div>
              <div><dt>Recovered quantity</dt><dd>${esc(((evRec.Recovered_quantities || {}).quantity) ?? "UNKNOWN")} ${esc(((evRec.Recovered_quantities || {}).uom) || "")}</dd></div>
              <div><dt>Product / Part</dt><dd>${esc(((evRec.Product_identifiers || {}).Manufacturer) ?? "UNKNOWN")} · ${esc(((evRec.Product_identifiers || {}).Part_number) ?? "UNKNOWN")}</dd></div>
              <div><dt>Completeness</dt><dd>${esc(evRec.Completeness_score ?? "UNKNOWN")}</dd></div>
              <div><dt>Commercial readiness</dt><dd><strong>${esc(evRec.Commercial_readiness ?? "0")}</strong></dd></div>
              <div><dt>Attachment status</dt><dd>${esc(evRec.Attachment_status ?? "UNKNOWN")}</dd></div>
              <div><dt>Source health</dt><dd>${esc(evRec.Source_health ?? "UNKNOWN")}</dd></div>
              <div><dt>Queue</dt><dd>${esc(evRec.Queue ?? "UNKNOWN")}</dd></div>
              <div><dt>Missing information</dt><dd>${esc(((evRec.Missing_information || []).join(", ")) || "none listed")}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(evRec.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">${esc(evRec.Readiness_explanation || "VA may attach evidence / escalate — no bids, outreach, or scoring changes")}</p>`
          ),
          sectionCard(
            "Evidence Chain",
            `<dl class="m3-kv">
              <div><dt>Original source</dt><dd>${esc(evChain.Original_source ?? "UNKNOWN")}</dd></div>
              <div><dt>Source id</dt><dd>${esc(evChain.Source_id ?? "UNKNOWN")}</dd></div>
              <div><dt>Discovery date</dt><dd>${esc(evChain.Discovery_date ?? "UNKNOWN")}</dd></div>
              <div><dt>Discovery run</dt><dd>${esc(evChain.Discovery_run_ID ?? "UNKNOWN")}</dd></div>
              <div><dt>Evidence confidence</dt><dd><strong>${esc(evChain.Evidence_confidence ?? "UNKNOWN")}</strong></dd></div>
              <div><dt>Evidence status</dt><dd>${esc(evChain.Evidence_status ?? "UNKNOWN")}</dd></div>
              <div><dt>Documents found</dt><dd>${esc(((evChain.Documents_found || []).length) || 0)}</dd></div>
              <div><dt>Documents missing</dt><dd>${esc(((evChain.Documents_missing || []).join(", ")) || "none listed")}</dd></div>
              <div><dt>Last recovery attempt</dt><dd>${esc(evChain.Last_recovery_attempt ?? "UNKNOWN")}</dd></div>
              <div><dt>Handoff</dt><dd>${esc(((evChain.Handoff_validation || {}).Discovery_to_Pipeline) ?? "UNKNOWN")} · loss ${esc(((evChain.Data_loss || {}).lost_count) ?? 0)}</dd></div>
              <div><dt>Next Action</dt><dd>${esc(evChain.Next_Action ?? "—")}</dd></div>
            </dl>
            <p class="muted">VA can review/attach/update/note — cannot bid, contact suppliers, or change scoring</p>`
          ),
          sectionCard(
            "Evidence",
            `<dl class="m3-kv">
              <div><dt>Package</dt><dd>${esc(ev.package_completeness)}</dd></div>
              <div><dt>Docs / bytes</dt><dd>${esc(ev.document_count)} / ${esc(ev.documents_with_bytes)}</dd></div>
              <div><dt>Governing</dt><dd>${esc(ev.governing_document)}</dd></div>
              <div><dt>BOM lines</dt><dd>${esc(ev.line_item_count)}</dd></div>
              <div><dt>Portal</dt><dd>${esc(ev.portal_family)}</dd></div>
              <div><dt>Deal type</dt><dd>${esc(ev.deal_type)}</dd></div>
              <div><dt>Failure</dt><dd>${esc(ev.primary_failure)}</dd></div>
              <div><dt>Access</dt><dd>${esc(ev.auth_requirements)}</dd></div>
              <div><dt>Next</dt><dd>${esc(ev.next_evidence_action)}</dd></div>
            </dl>
            <p class="muted">Recovery tiers: ${esc(((ev.recovery_attempts || []).map((a) => a.tier).join(", ")) || "none yet")}</p>`
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
            "Commercial Research",
            `<dl class="m3-kv">
              <div><dt>MSRP/list</dt><dd>${esc(commercial.msrp_list)}</dd></div>
              <div><dt>Lowest public new</dt><dd>${esc(commercial.lowest_public_new_price)}</dd></div>
              <div><dt>Gov historical</dt><dd>${esc(commercial.government_historical_price)}</dd></div>
              <div><dt>Acquisition target</dt><dd>${esc(commercial.acquisition_target)}</dd></div>
              <div><dt>Wholesale status</dt><dd>${esc(commercial.wholesale_verification_status)}</dd></div>
              <div><dt>Competition</dt><dd>${esc(((commercial.historical_winners || {}).signal) || "UNKNOWN")}</dd></div>
            </dl>
            <p class="muted">Public price failure ≠ economic failure. Wholesale verification may be required.</p>`
          ),
          sectionCard(
            "Source Access",
            `<dl class="m3-kv">
              <div><dt>State</dt><dd>${esc(srcAccess.state)}</dd></div>
              <div><dt>Credentials</dt><dd>${srcAccess.credentials_available ? "yes" : "no"}</dd></div>
              <div><dt>Registration</dt><dd>${srcAccess.registration_required ? "required" : "not required"}</dd></div>
              <div><dt>Login</dt><dd>${esc(srcAccess.login_status)}</dd></div>
            </dl>`
          ),
          sectionCard(
            "Phase L Access / Competition / Economics",
            (() => {
              const pl = deal.phase_l_owner_view || deal.phase_l || {};
              const a = pl.access || {};
              const c = pl.competition || {};
              const e = pl.economics || {};
              const s = pl.source || {};
              return `<dl class="m3-kv">
              <div><dt>Can we bid?</dt><dd>${esc(a.can_we_bid || "—")}</dd></div>
              <div><dt>Why / blocker</dt><dd>${esc(a.why || "—")}</dd></div>
              <div><dt>Access type</dt><dd>${esc(a.competition_access_type || "—")}</dd></div>
              <div><dt>Hist offers</dt><dd>${esc(c.historical_offers ?? "—")}</dd></div>
              <div><dt>Competition signal</dt><dd>${esc(c.effective_competition_signal || "—")}</dd></div>
              <div><dt>Retail unit</dt><dd>${esc(e.public_retail_unit_price ?? "—")}</dd></div>
              <div><dt>Expected net</dt><dd>${esc(e.expected_net_profit ?? "—")}</dd></div>
              <div><dt>Profit tier</dt><dd>${esc(e.profit_tier || "—")}</dd></div>
              <div><dt>Source level</dt><dd>${esc(s.source_level || "—")}</dd></div>
              <div><dt>Owner decision</dt><dd>${esc(pl.owner_decision || "—")}</dd></div>
            </dl>`;
            })()
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
      if (el.matches("button")) return;
      const go = (e) => {
        if (e && e.target && e.target.closest && e.target.closest("[data-stop],button")) return;
        const cid = el.getAttribute("data-cid");
        if (cid) openDealRoom(cid);
      };
      el.addEventListener("click", go);
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go(e);
        }
      });
    });
  }

  function wireOppActions(container) {
    if (!container) return;
    container.querySelectorAll(".m3-advance-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const cid = btn.getAttribute("data-cid");
        if (cid) openDealRoom(cid);
      });
    });
    container.querySelectorAll(".m3-reject-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const card = btn.closest(".m3-opp-card");
        if (card) {
          card.classList.add("m3-rejected");
          card.innerHTML = "<p class='muted'>Marked reject (local only — not persisted yet).</p>";
        }
      });
    });
  }

  function wireNav() {
    document.querySelectorAll(".m3-nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = btn.dataset.m3View;
        showM3View(v);
      });
    });
    document.getElementById("m3-deal-back")?.addEventListener("click", () => showM3View("deep-dive"));
    document.getElementById("m3-advanced-back")?.addEventListener("click", () => showM3View("settings"));
    document.getElementById("m3-open-verify")?.addEventListener("click", () => showM3View("verify"));
    document.getElementById("m3-open-advanced")?.addEventListener("click", () => showM3View("advanced"));
    document.getElementById("m3-open-actions")?.addEventListener("click", () => showM3View("actions"));
    document.getElementById("m3-open-sources")?.addEventListener("click", () => showM3View("sources"));
    document.getElementById("m3-open-micro-lab")?.addEventListener("click", () => showM3View("micro-lab"));
    document.getElementById("m3-role-owner")?.addEventListener("click", () => setRole("owner"));
    document.getElementById("m3-role-va")?.addEventListener("click", () => setRole("va"));
    document.querySelectorAll(".m3-opp-filter").forEach((btn) => {
      btn.addEventListener("click", () => {
        portfolioFilter = btn.getAttribute("data-filter") || "intake";
        loadOpportunities(false);
      });
    });
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
        await postJson("/api/m3/learning/financing-verification", {
          record_id: activeLearningRecordId,
          financing_path: document.getElementById("m3-fv-path")?.value || "UNKNOWN",
          result: document.getElementById("m3-fv-state")?.value || "UNKNOWN",
          state: document.getElementById("m3-fv-state")?.value || "UNKNOWN",
          authorized_by: "mobile-operator",
        });
        setLearningMsg("Financing verification recorded");
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
    document.body.classList.add("m3-only-app", "m3-mobile-active");
    const h = (location.hash || "").replace("#", "");
    if (h.startsWith("m3-")) {
      const view = h.replace(/^m3-/, "") || "home";
      showM3View(M3_VIEWS.includes(view) ? view : "home");
    } else {
      // Clear legacy gos-* hashes that previously hijacked navigation
      if (/^gos-/.test(h) || h === "dashboard" || h === "today") {
        try {
          history.replaceState(null, "", "#m3-home");
        } catch (_) {}
      }
      showM3View("home");
    }
  }

  window.M3Mobile = {
    showM3View,
    openDealRoom,
    loadHome,
    loadVerify,
    refresh: () => {
      cache = { dashboard: null, actions: null, sources: null, deal: null, mode: null, pursuits: null, learning: null, profile: null };
      return loadHome(true);
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
