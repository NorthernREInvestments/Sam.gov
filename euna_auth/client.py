"""Autonomous Euna Supplier Network authenticated Playwright client.

Real login flow (verified):
  vendor.bonfirehub.com/login?bounceUrl=/opportunities
  → account.bonfirehub.com/login?flow=<uuid>  (email + Continue)
  → same flow URL with password + Log In
  → vendor.bonfirehub.com/opportunities
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from euna_auth.classify import classify_login_failure, detect_account_activation, is_authenticated_page
from euna_auth.config import load_euna_auth_config
from euna_auth.session_store import load_storage_state, save_storage_state
from euna_auth.states import (
    AUTH_CHALLENGE,
    AUTH_FAILED,
    DISABLED,
    LOGIN_SUCCESS,
    NETWORK_TIMEOUT,
    SESSION_REUSED,
    SSO_REQUIRED,
    UNKNOWN_AUTH_FAILURE,
    WRONG_FORM_FLOW,
)
from euna_auth.telemetry import record_auth_event

log = logging.getLogger("govtracker.euna_auth.client")

_CHALLENGE_RE = re.compile(
    r"captcha|recaptcha|hcaptcha|two[- ]factor|mfa|verification code|cloudflare|just a moment",
    re.I,
)


@dataclass
class AuthResult:
    status: str
    authenticated: bool
    message: str | None = None
    challenge_type: str | None = None
    reused_session: bool = False
    failure_reason: str | None = None
    account_state: str | None = None
    flow_detected: str | None = None
    auth_host: str | None = None
    visible_error: str | None = None
    diagnostic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "authenticated": self.authenticated,
            "message": self.message,
            "challenge_type": self.challenge_type,
            "reused_session": self.reused_session,
            "failure_reason": self.failure_reason or self.message,
            "account_state": self.account_state,
            "flow_detected": self.flow_detected,
            "auth_host": self.auth_host,
            "visible_error": self.visible_error,
            "diagnostic": self.diagnostic,
        }


class EunaAuthenticatedClient:
    """Chromium session for Euna Supplier Network (central vendor network)."""

    def __init__(self, config: Any | None = None) -> None:
        self.config = config or load_euna_auth_config()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.last_result: AuthResult | None = None
        self.last_trace: list[dict[str, Any]] = []

    def __enter__(self) -> "EunaAuthenticatedClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def is_authenticated(self) -> bool:
        return bool(self.last_result and self.last_result.authenticated)

    @property
    def page(self) -> Any:
        return self._page

    def close(self) -> None:
        for attr in ("_page", "_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _trace(self, **kwargs: Any) -> None:
        row = {k: v for k, v in kwargs.items() if v is not None}
        # Never store secrets
        row.pop("username", None)
        row.pop("password", None)
        self.last_trace.append(row)

    def ensure_authenticated(self) -> AuthResult:
        cfg = self.config
        if not cfg.auth_enabled:
            result = AuthResult(status=DISABLED, authenticated=False, message="EUNA_AUTH_ENABLED=false")
            self.last_result = result
            record_auth_event(DISABLED, failure_reason="disabled")
            return result
        if not cfg.credentials_present:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="EUNA_USERNAME/EUNA_PASSWORD not configured",
                failure_reason="CREDENTIALS_MISSING",
            )
            self.last_result = result
            record_auth_event(AUTH_FAILED, failure_reason=result.failure_reason, login_failure=True)
            return result
        try:
            self._ensure_browser()
        except Exception as exc:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"browser_unavailable:{type(exc).__name__}",
                failure_reason=NETWORK_TIMEOUT if "Timeout" in type(exc).__name__ else UNKNOWN_AUTH_FAILURE,
            )
            self.last_result = result
            record_auth_event(AUTH_FAILED, failure_reason=result.failure_reason, login_failure=True)
            return result

        if load_storage_state() is not None:
            probe = self._probe_authenticated()
            if probe.get("challenge"):
                result = AuthResult(
                    status=AUTH_CHALLENGE,
                    authenticated=False,
                    message=probe.get("challenge_type") or "challenge_detected",
                    challenge_type=probe.get("challenge_type"),
                    failure_reason=probe.get("challenge_type") or AUTH_CHALLENGE,
                    auth_host=probe.get("auth_host"),
                )
                self.last_result = result
                record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=result.failure_reason)
                return result
            if probe.get("authenticated"):
                result = AuthResult(
                    status=SESSION_REUSED,
                    authenticated=True,
                    message="existing session valid",
                    reused_session=True,
                    account_state=probe.get("account_state"),
                    flow_detected="session_reuse",
                    auth_host=probe.get("auth_host"),
                )
                self.last_result = result
                record_auth_event(SESSION_REUSED, session_reuse=True)
                self._persist_state()
                return result

        login = self._perform_login()
        self.last_result = login
        return login

    def _ensure_browser(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        from playwright_bootstrap import ensure_playwright_chromium

        ensure_playwright_chromium()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.config.headless)
        kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        state = load_storage_state()
        if state:
            kwargs["storage_state"] = state
        self._context = self._browser.new_context(**kwargs)
        self._page = self._context.new_page()

    def _persist_state(self) -> None:
        if self._context is None:
            return
        try:
            save_storage_state(self._context.storage_state())
        except Exception:
            log.exception("Failed saving Euna storage state")

    def _page_bits(self) -> dict[str, Any]:
        assert self._page is not None
        html = ""
        text = ""
        title = ""
        url = ""
        try:
            url = self._page.url or ""
            title = self._page.title() or ""
            html = self._page.content() or ""
            text = self._page.inner_text("body") or ""
        except Exception as exc:
            return {"url": url, "title": title, "html": html, "text": text, "error": type(exc).__name__}
        return {"url": url, "title": title, "html": html, "text": text[:4000]}

    def _probe_authenticated(self) -> dict[str, Any]:
        assert self._page is not None
        cfg = self.config
        try:
            self._page.goto(cfg.verify_url, wait_until="domcontentloaded", timeout=60_000)
            # SPA entitlement copy ("Sign up for Euna Supplier Network") renders after shell
            try:
                self._page.wait_for_timeout(2000)
            except Exception:
                pass
        except Exception as exc:
            return {"authenticated": False, "error": type(exc).__name__}
        bits = self._page_bits()
        html, url, text = bits.get("html") or "", bits.get("url") or "", bits.get("text") or ""
        if _CHALLENGE_RE.search(html):
            return {
                "authenticated": False,
                "challenge": True,
                "challenge_type": "challenge_page",
                "auth_host": url.split("/")[2] if "://" in url else None,
            }
        if is_authenticated_page(html=html, url=url, visible_text=text):
            return {
                "authenticated": True,
                "account_state": detect_account_activation(html, visible_text=text, url=url),
                "auth_host": url.split("/")[2] if "://" in url else None,
            }
        return {"authenticated": False, "auth_host": url.split("/")[2] if "://" in url else None}

    def _fail_from_page(self, *, exception_name: str | None = None) -> AuthResult:
        bits = self._page_bits()
        html = bits.get("html") or ""
        url = bits.get("url") or ""
        text = bits.get("text") or ""
        had_pw = bool(re.search(r'type=["\']password["\']', html, re.I))
        had_email = bool(re.search(r'type=["\']email["\']|#input-email|name=["\']email["\']', html, re.I))
        flow_present = "flow=" in url.lower() or bool(re.search(r'name=["\']flowId["\']', html, re.I))
        classified = classify_login_failure(
            html=html,
            url=url,
            visible_text=text,
            exception_name=exception_name,
            had_password_field=had_pw,
            had_email_field=had_email,
            flow_present=flow_present,
        )
        reason = classified.get("reason") or UNKNOWN_AUTH_FAILURE
        status = AUTH_CHALLENGE if reason in {SSO_REQUIRED, "MFA_REQUIRED", "CAPTCHA"} else AUTH_FAILED
        if reason in {"MFA_REQUIRED", "CAPTCHA", SSO_REQUIRED}:
            status = AUTH_CHALLENGE
        result = AuthResult(
            status=status,
            authenticated=False,
            message=reason,
            failure_reason=reason,
            challenge_type=reason if status == AUTH_CHALLENGE else None,
            account_state=classified.get("account_state"),
            flow_detected=classified.get("flow_detected"),
            auth_host=classified.get("auth_host"),
            visible_error=classified.get("visible_error"),
            diagnostic={
                "final_url": url[:240],
                "page_title": (bits.get("title") or "")[:120],
                "login_page_visible": "login" in url.lower(),
                "password_field_visible": had_pw,
                "email_field_visible": had_email,
                "flow_present": flow_present,
                "trace": self.last_trace[-12:],
            },
        )
        record_auth_event(
            status,
            login_failure=status == AUTH_FAILED,
            challenge=status == AUTH_CHALLENGE,
            failure_reason=reason,
        )
        return result

    def _perform_login(self) -> AuthResult:
        assert self._page is not None
        cfg = self.config
        self.last_trace = []
        record_auth_event("LOGIN_REQUIRED", login_attempt=True)

        start_url = cfg.login_url
        self._trace(step="goto_start", url=start_url)
        try:
            self._page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=NETWORK_TIMEOUT)
            return AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=type(exc).__name__,
                failure_reason=NETWORK_TIMEOUT,
            )

        # Allow bounce into account.bonfirehub.com/login?flow=...
        try:
            self._page.wait_for_url(re.compile(r"account\.bonfirehub\.com/login"), timeout=20_000)
        except Exception:
            pass
        try:
            self._page.wait_for_timeout(800)
        except Exception:
            pass

        bits = self._page_bits()
        url = bits.get("url") or ""
        html = bits.get("html") or ""
        self._trace(step="after_start", url=url[:240], title=(bits.get("title") or "")[:80])

        if _CHALLENGE_RE.search(html) and "password" not in html.lower() and "email" not in html.lower():
            return self._fail_from_page()

        if "flow=" not in url.lower() and "account.bonfirehub.com" not in url:
            # Try explicit vendor login if we landed elsewhere
            try:
                self._page.goto(
                    "https://vendor.bonfirehub.com/login?bounceUrl=%2Fopportunities",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                self._page.wait_for_url(re.compile(r"account\.bonfirehub\.com/login"), timeout=20_000)
            except Exception:
                pass
            bits = self._page_bits()
            url = bits.get("url") or ""
            html = bits.get("html") or ""
            self._trace(step="vendor_bounce_retry", url=url[:240])

        try:
            # Step 1: email
            email = self._page.locator(
                "#input-email, input[type='email'], input[name='email'], "
                "input[name*='email' i], input[placeholder*='email' i]"
            ).first
            email.wait_for(state="visible", timeout=20_000)
            email.fill(cfg.username, timeout=10_000)
            self._trace(step="email_filled", username_length=len(cfg.username))

            # If password not yet visible → Continue
            pw_loc = self._page.locator("#input-password, input[type='password']")
            if pw_loc.count() == 0 or not pw_loc.first.is_visible():
                cont = self._page.get_by_role("button", name=re.compile(r"^continue$", re.I))
                if cont.count() == 0:
                    cont = self._page.locator("button[type='submit']")
                cont.first.click(timeout=10_000)
                self._trace(step="continue_clicked")
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                # Wait for password OR SSO OR error
                try:
                    self._page.locator("#input-password, input[type='password']").first.wait_for(
                        state="visible", timeout=25_000
                    )
                except Exception:
                    bits = self._page_bits()
                    # SSO / magic link / username not found after Continue
                    if re.search(r"microsoft|google|okta|sso|single\s+sign", bits.get("text") or "", re.I):
                        return self._fail_from_page()
                    return self._fail_from_page(exception_name="TimeoutError")

            # Step 2: password + Log In
            pw = self._page.locator("#input-password, input[type='password']").first
            pw.wait_for(state="visible", timeout=15_000)
            pw.fill(cfg.password, timeout=10_000)
            self._trace(step="password_filled", password_length=len(cfg.password))

            login_btn = self._page.get_by_role("button", name=re.compile(r"^log\s*in$", re.I))
            if login_btn.count() == 0:
                login_btn = self._page.get_by_role(
                    "button", name=re.compile(r"sign\s*in|submit|continue", re.I)
                )
            if login_btn.count() == 0:
                login_btn = self._page.locator("button[type='submit']")
            login_btn.first.click(timeout=10_000)
            self._trace(step="login_clicked")

            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=45_000)
            except Exception:
                pass
            try:
                # Prefer leaving the login host
                self._page.wait_for_url(
                    re.compile(r"vendor\.bonfirehub\.com(?!.*login)|account\.bonfirehub\.com/(?!login)"),
                    timeout=25_000,
                )
            except Exception:
                pass
            try:
                self._page.wait_for_timeout(1200)
            except Exception:
                pass
        except Exception as exc:
            self._trace(step="login_exception", error=type(exc).__name__)
            return self._fail_from_page(exception_name=type(exc).__name__)

        bits = self._page_bits()
        html = bits.get("html") or ""
        url = bits.get("url") or ""
        text = bits.get("text") or ""
        self._trace(step="post_login", url=url[:240], title=(bits.get("title") or "")[:80])

        if is_authenticated_page(html=html, url=url, visible_text=text):
            # Anchor on CF-safe page (/agencies) — /opportunities is Cloudflare-gated headless
            try:
                if "agencies" not in url.lower() and "dashboard" not in url.lower():
                    self._page.goto(
                        cfg.verify_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    bits = self._page_bits()
                    url = bits.get("url") or url
                    html = bits.get("html") or html
                    text = bits.get("text") or text
            except Exception:
                pass
            if not is_authenticated_page(html=html, url=url, visible_text=text):
                return self._fail_from_page()
            self._persist_state()
            account_state = detect_account_activation(html, visible_text=text, url=url)
            auth_host = url.split("/")[2] if "://" in url else None
            record_auth_event(
                LOGIN_SUCCESS,
                login_success=True,
                account_state=account_state,
                flow_detected="email_continue_password",
                auth_host=auth_host,
            )
            return AuthResult(
                status=LOGIN_SUCCESS,
                authenticated=True,
                message="login_ok",
                account_state=account_state,
                flow_detected="email_continue_password",
                auth_host=auth_host,
                diagnostic={
                    "final_url": url[:240],
                    "page_title": (bits.get("title") or "")[:120],
                    "trace": self.last_trace[-12:],
                },
            )

        # Still on login / challenge
        if _CHALLENGE_RE.search(html):
            return self._fail_from_page()
        return self._fail_from_page()


def test_connection() -> dict[str, Any]:
    cfg = load_euna_auth_config()
    with EunaAuthenticatedClient(cfg) as client:
        auth = client.ensure_authenticated()
        from euna_auth.telemetry import owner_connection_status

        return {
            "ok": auth.authenticated,
            "auth": auth.to_dict(),
            "connection": owner_connection_status(),
            "credential_hygiene": cfg.credential_hygiene(),
        }


def run_auth_diagnostic(*, persist_screenshot: bool = True) -> dict[str, Any]:
    """Stage A diagnostic — sanitized login outcome for owner/ops."""
    from application_clock import now_utc
    from m3_data_root import data_path

    cfg = load_euna_auth_config()
    report: dict[str, Any] = {
        "kind": "EunaAuthDiagnostic",
        "started_at": now_utc().isoformat(),
        "credential_hygiene": cfg.credential_hygiene(),
        "start_url": cfg.login_url,
        "verify_url": cfg.verify_url,
    }
    with EunaAuthenticatedClient(cfg) as client:
        auth = client.ensure_authenticated()
        report["auth"] = auth.to_dict()
        bits = {}
        if client.page is not None:
            try:
                bits = {
                    "final_url": (client.page.url or "")[:240],
                    "page_title": (client.page.title() or "")[:120],
                }
                text = (client.page.inner_text("body") or "")[:1500]
                report["visible_text_sample"] = re.sub(
                    r"(?i)(password|token|cookie)\s*[:=]\s*\S+",
                    r"\1=[REDACTED]",
                    text,
                )
                if persist_screenshot:
                    shot = data_path("euna_auth/last_auth_diagnostic.png")
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    client.page.screenshot(path=str(shot), full_page=False)
                    report["screenshot_path"] = str(shot)
            except Exception as exc:
                report["page_capture_error"] = type(exc).__name__
        report.update(bits)
        report["trace"] = client.last_trace[-20:]
        report["login_success"] = bool(auth.authenticated)
        report["failure_reason"] = auth.failure_reason
        report["account_state"] = auth.account_state
        report["flow_detected"] = auth.flow_detected
        report["auth_host"] = auth.auth_host
    report["completed_at"] = now_utc().isoformat()
    try:
        path = data_path("euna_auth/last_auth_diagnostic.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    except Exception:
        pass
    return report
