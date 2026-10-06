"""Reusable OpenGov authenticated Playwright client."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from opengov_auth.config import OpenGovAuthConfig, load_opengov_auth_config
from opengov_auth.session_store import load_storage_state, save_storage_state, storage_state_path
from opengov_auth.states import (
    AUTH_CHALLENGE,
    AUTH_FAILED,
    AUTHENTICATED_STATUSES,
    CONNECTED,
    DISABLED,
    LOGIN_REQUIRED,
    LOGIN_SUCCESS,
    SESSION_REUSED,
)
from opengov_auth.telemetry import record_auth_event

log = logging.getLogger("govtracker.opengov_auth.client")

_CHALLENGE_PATTERNS = re.compile(
    r"(captcha|recaptcha|hcaptcha|g-recaptcha|cf-challenge|challenge-platform|"
    r"two[\-\s]?factor|multi[\-\s]?factor|\bmfa\b|one[\-\s]?time\s*code|"
    r"verification\s*code|email\s*verification|verify\s*your\s*(email|identity)|"
    r"unusual\s*activity|human\s*verification|bot\s*detection|"
    r"are\s*you\s*a\s*robot|security\s*check|just\s*a\s*moment)",
    re.I,
)

_AUTH_POSITIVE = re.compile(
    r"(log\s*out|sign\s*out|my\s*account|vendor\s*dashboard|network|"
    r"subscriptions|account\s*settings|welcome,\s*|supplier)",
    re.I,
)

_AUTH_NEGATIVE = re.compile(
    r"(/login|sign\s*in\s*to\s*your\s*account|vendor\s*login|"
    r"invalid\s*(username|password|credentials)|forgot\s*(your\s*)?password)",
    re.I,
)


@dataclass
class AuthResult:
    status: str
    authenticated: bool
    message: str = ""
    reused_session: bool = False
    challenge_type: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "authenticated": self.authenticated,
            "message": self.message,
            "reused_session": self.reused_session,
            "challenge_type": self.challenge_type,
            "details": self.details,
        }


class OpenGovAuthenticatedClient:
    """Launch Chromium, reuse/login OpenGov vendor session, fetch pages/APIs."""

    def __init__(self, config: OpenGovAuthConfig | None = None) -> None:
        self.config = config or load_opengov_auth_config()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.last_result: AuthResult | None = None
        self._captured_json: list[dict[str, Any]] = []
        self._resource_blocking = False

    def __enter__(self) -> "OpenGovAuthenticatedClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def is_authenticated(self) -> bool:
        return bool(self.last_result and self.last_result.authenticated)

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

    def ensure_authenticated(self) -> AuthResult:
        cfg = self.config
        if not cfg.auth_enabled:
            result = AuthResult(status=DISABLED, authenticated=False, message="OPENGOV_AUTH_ENABLED=false")
            self.last_result = result
            record_auth_event(DISABLED, failure_reason="disabled")
            return result
        if not cfg.credentials_present:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="OPENGOV_USERNAME/OPENGOV_PASSWORD not configured",
            )
            self.last_result = result
            record_auth_event(AUTH_FAILED, failure_reason=result.message, login_failure=True)
            return result

        try:
            self._ensure_browser()
        except Exception as exc:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"browser_unavailable:{type(exc).__name__}",
            )
            self.last_result = result
            record_auth_event(AUTH_FAILED, failure_reason=result.message, login_failure=True)
            log.warning("OpenGov browser unavailable: %s", type(exc).__name__)
            return result

        if load_storage_state() is not None:
            probe = self._probe_authenticated()
            if probe.get("challenge"):
                result = AuthResult(
                    status=AUTH_CHALLENGE,
                    authenticated=False,
                    message=probe.get("challenge_type") or "challenge_detected",
                    challenge_type=probe.get("challenge_type"),
                )
                self.last_result = result
                record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=result.message)
                return result
            if probe.get("authenticated"):
                result = AuthResult(
                    status=SESSION_REUSED,
                    authenticated=True,
                    message="existing session valid",
                    reused_session=True,
                )
                self.last_result = result
                record_auth_event(SESSION_REUSED, session_reuse=True)
                self._persist_state()
                return result
            log.info("OpenGov stored session expired — automatic login required")
            record_auth_event("EXPIRED", failure_reason="session_expired")

        login = self._perform_login()
        self.last_result = login
        return login

    def enable_resource_blocking(self) -> None:
        """Block images/fonts/media to reduce portal timeouts. Safe for JSON/HTML harvest."""
        if self._resource_blocking or self._page is None:
            return

        def _route(route: Any) -> None:
            try:
                if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            self._page.route("**/*", _route)
            self._resource_blocking = True
        except Exception:
            pass

    def fetch_html(self, url: str, *, timeout_ms: int = 45_000) -> str:
        if not self.is_authenticated:
            raise RuntimeError("OpenGov client is not authenticated")
        assert self._page is not None
        self._captured_json = []
        # Do not wait for full networkidle — useful data often arrives earlier
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            self._page.wait_for_timeout(2_000)
        except Exception:
            pass
        # Targeted content waits
        for sel in (
            "text=/project|opportunity|solicitation|proposal/i",
            "[class*='project']",
            "a[href*='/projects/']",
            "table",
        ):
            try:
                self._page.wait_for_selector(sel, timeout=8_000)
                break
            except Exception:
                continue
        html = self._page.content()
        challenge = self._detect_challenge(html, self._page.url)
        # Soft-wait once for Cloudflare interstitial; hard-fail only on captcha/MFA
        if challenge and re.search(r"just\s*a\s*moment|challenge-platform", challenge, re.I):
            try:
                self._page.wait_for_timeout(5_000)
            except Exception:
                pass
            html = self._page.content()
            challenge = self._detect_challenge(html, self._page.url)
        if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha|hcaptcha", challenge, re.I):
            raise RuntimeError(f"AUTH_CHALLENGE:{challenge}")
        # Soft CDN interstitial is not a hard auth challenge for portal classification
        return html

    def fetch_json(
        self,
        url: str,
        *,
        timeout_ms: int = 60_000,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> Any | None:
        if not self.is_authenticated or self._context is None:
            return None
        try:
            # government/{code}/project/public is POST-only
            use_post = method.upper() == "POST" or bool(
                re.search(r"/government/[^/]+/project/public/?$", url or "", re.I)
            )
            if use_post:
                payload = json_body if json_body is not None else {"limit": 50, "offset": 0}
                resp = self._context.request.post(
                    url,
                    data=json.dumps(payload),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Origin": "https://procurement.opengov.com",
                        "Referer": "https://procurement.opengov.com/",
                    },
                    timeout=timeout_ms,
                )
            else:
                resp = self._context.request.get(url, timeout=timeout_ms)
            if not resp.ok:
                return None
            return resp.json()
        except Exception as exc:
            log.warning("OpenGov JSON fetch failed: %s", type(exc).__name__)
            return None

    def download_bytes(self, url: str, *, timeout_ms: int = 90_000) -> bytes | None:
        if not self.is_authenticated or self._context is None:
            return None
        try:
            resp = self._context.request.get(url, timeout=timeout_ms)
            if resp.ok:
                return resp.body()
        except Exception as exc:
            log.warning("OpenGov download failed: %s", type(exc).__name__)
        return None

    def navigate_and_collect_json(self, url: str, *, timeout_ms: int = 90_000) -> tuple[str, list[Any]]:
        """Navigate URL, return HTML + JSON payloads captured from XHR/fetch."""
        assert self._page is not None
        captured: list[Any] = []

        def _on_response(response: Any) -> None:
            try:
                ctype = (response.headers or {}).get("content-type", "")
                u = response.url or ""
                if "application/json" not in ctype and "/api/" not in u.lower():
                    return
                if response.status != 200:
                    return
                data = response.json()
                captured.append({"url": u, "data": data})
            except Exception:
                pass

        self._page.on("response", _on_response)
        try:
            html = self.fetch_html(url, timeout_ms=timeout_ms)
        finally:
            try:
                self._page.remove_listener("response", _on_response)
            except Exception:
                pass
        self._captured_json = captured
        return html, captured

    # --- internals ---------------------------------------------------------

    def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright

        from playwright_bootstrap import ensure_playwright_chromium

        ensure_playwright_chromium()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.config.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        state = load_storage_state()
        kwargs: dict[str, Any] = {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1400, "height": 900},
        }
        if state:
            kwargs["storage_state"] = state
        self._context = self._browser.new_context(**kwargs)
        self._page = self._context.new_page()

    def _probe_authenticated(self) -> dict[str, Any]:
        assert self._page is not None
        try:
            self._page.goto(self.config.verify_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        except Exception as exc:
            return {"authenticated": False, "error": type(exc).__name__}

        html = self._page.content()
        current = self._page.url or ""
        challenge = self._detect_challenge(html, current)
        if challenge and "just a moment" in challenge:
            return {"authenticated": False, "challenge": True, "challenge_type": challenge}
        if challenge and re.search(r"captcha|mfa|two.?factor", challenge, re.I):
            return {"authenticated": False, "challenge": True, "challenge_type": challenge}
        if self._looks_authenticated(html, current):
            return {"authenticated": True}
        return {"authenticated": False}

    def _looks_authenticated(self, html: str, url: str) -> bool:
        u = (url or "").lower()
        if "/login" in u and "vendor" not in html.lower()[:500]:
            # still on login
            if re.search(r'type=["\']password["\']', html or "", re.I):
                return False
        if self._page is not None:
            try:
                if self._page.locator('a[href*="logout"], a[href*="signout"], button:has-text("Log out")').count() > 0:
                    return True
                if self._page.get_by_role("link", name=re.compile(r"log\s*out|sign\s*out", re.I)).count() > 0:
                    return True
                # Vendor network / subscriptions markers
                if self._page.get_by_text(re.compile(r"my\s*subscriptions|vendor\s*network|find\s*bids", re.I)).count() > 0:
                    if "/login" not in u:
                        return True
            except Exception:
                pass
        if "/login" in u:
            return False
        if _AUTH_POSITIVE.search(html or "") and not re.search(r'type=["\']password["\'].*Vendor Login', html or "", re.I | re.S):
            # Prefer positive markers away from login form
            if not re.search(r'Sign in to your account', html or "", re.I):
                return True
        return False

    def _detect_challenge(self, html: str, url: str) -> str | None:
        blob = f"{url or ''}\n{html or ''}"
        m = _CHALLENGE_PATTERNS.search(blob)
        if not m:
            if re.search(r"<iframe[^>]+(recaptcha|hcaptcha|captcha)", html or "", re.I):
                return "captcha_iframe"
            return None
        return m.group(0).lower()[:80]

    def _safe_flow_diag(self, *, stage: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Safe auth diagnostics — never includes credentials/cookies/tokens."""
        page = self._page
        url = ""
        title = ""
        labels: list[str] = []
        try:
            url = (page.url if page else "") or ""
            if page is not None:
                try:
                    title = page.title() or ""
                except Exception:
                    title = ""
                for name in ("Vendor Login", "Email", "Password", "Continue", "Login", "Sign in", "Back"):
                    try:
                        if page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)).count() > 0:
                            labels.append(name)
                        elif page.get_by_label(re.compile(rf"^{re.escape(name)}$", re.I)).count() > 0:
                            labels.append(name)
                        elif page.get_by_placeholder(re.compile(name, re.I)).count() > 0:
                            labels.append(name)
                    except Exception:
                        continue
        except Exception:
            pass
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        diag = {
            "stage": stage,
            "url": url[:180],
            "host": host,
            "title": (title or "")[:120],
            "labels": labels[:12],
        }
        if extra:
            diag.update(extra)
        return diag

    def _click_vendor_login(self) -> bool:
        assert self._page is not None
        for getter in (
            lambda: self._page.get_by_role("button", name=re.compile(r"vendor\s*login", re.I)).first,
            lambda: self._page.get_by_role("link", name=re.compile(r"vendor\s*login", re.I)).first,
            lambda: self._page.get_by_text(re.compile(r"^\s*Vendor Login\s*$", re.I)).first,
            lambda: self._page.locator("button:has-text('Vendor Login'), a:has-text('Vendor Login')").first,
        ):
            try:
                loc = getter()
                if loc.count() > 0 and loc.is_visible(timeout=2_000):
                    loc.click(timeout=10_000)
                    try:
                        self._page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        return False

    def _click_continue(self) -> bool:
        assert self._page is not None
        for name in (r"^\s*continue\s*$", r"^\s*next\s*$"):
            try:
                btn = self._page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.count() > 0 and btn.is_visible(timeout=1_500):
                    btn.click(timeout=10_000)
                    return True
            except Exception:
                continue
        return False

    def _wait_for_locator(self, finder, *, tries: int = 12, delay_ms: int = 800) -> Any:
        for _ in range(tries):
            loc = finder()
            if loc is not None:
                return loc
            try:
                self._page.wait_for_timeout(delay_ms)
            except Exception:
                break
        return finder()

    def _perform_login(self) -> AuthResult:
        """Multi-step OpenGov vendor login: Vendor Login → email → Continue → password → Login."""
        assert self._page is not None
        cfg = self.config
        record_auth_event(LOGIN_REQUIRED, login_attempt=True)
        redirect_hosts: list[str] = []
        flow_stages: list[dict[str, Any]] = []

        def _track(stage: str, **extra: Any) -> None:
            from urllib.parse import urlparse

            d = self._safe_flow_diag(stage=stage, extra=extra or None)
            host = d.get("host") or ""
            if host and host not in redirect_hosts:
                redirect_hosts.append(host)
            flow_stages.append(d)
            log.info(
                "OpenGov login flow stage=%s host=%s title=%s labels=%s",
                stage,
                host,
                d.get("title"),
                ",".join(d.get("labels") or []),
            )

        try:
            self._page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        except Exception as exc:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"login_navigation_failed:{type(exc).__name__}",
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result

        _track("start", start_url=(cfg.login_url or "")[:180])
        html = self._page.content()
        challenge = self._detect_challenge(html, self._page.url)
        if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha", challenge, re.I):
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=challenge,
                challenge_type=challenge,
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=challenge)
            log.warning("OpenGov AUTH_CHALLENGE during login: %s", challenge)
            return result

        # Step 1: Vendor Login entry (fields are NOT on the landing /login page)
        user_sel = self._find_username_locator()
        if user_sel is None:
            clicked = self._click_vendor_login()
            _track("vendor_login_click", clicked=clicked)
            if self._context is not None and len(self._context.pages) > 1:
                self._page = self._context.pages[-1]
                _track("popup_or_new_page")
            # Also handle iframe auth hosts
            try:
                for frame in self._page.frames:
                    if frame == self._page.main_frame:
                        continue
                    fu = (frame.url or "").lower()
                    if any(x in fu for x in ("login", "auth", "openid", "okta", "auth0")):
                        # Prefer main page locators; frame noted for diagnostics
                        _track("auth_iframe", iframe_host=fu[:80])
            except Exception:
                pass

        user_sel = self._wait_for_locator(self._find_username_locator)
        if user_sel is None:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="email_field_not_found",
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason="email_field_not_found")
            return result

        left_login = False
        try:
            user_sel.fill(cfg.username, timeout=15_000)
            _track("email_filled", email_field_found=True)
        except Exception as exc:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"email_fill_error:{type(exc).__name__}",
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result

        # Step 2: password may be same-page OR after Continue (email-first)
        pass_sel = self._find_password_locator()
        if pass_sel is None:
            continued = self._click_continue()
            _track("continue_clicked", continued=continued)
            try:
                self._page.wait_for_timeout(800)
            except Exception:
                pass
            pass_sel = self._wait_for_locator(self._find_password_locator)

        if pass_sel is None:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="password_field_not_found",
                details={
                    "redirect_hosts": redirect_hosts,
                    "flow": flow_stages,
                    "email_field_found": True,
                    "password_field_found": False,
                },
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason="password_field_not_found")
            return result

        try:
            pass_sel.fill(cfg.password, timeout=15_000)
            _track("password_filled", password_field_found=True)
            if not self._submit_login():
                result = AuthResult(
                    status=AUTH_FAILED,
                    authenticated=False,
                    message="login_submit_not_found",
                    details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
                )
                record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
                return result
            _track("login_submitted")
            # Auth0 / vendor redirect can take several seconds
            for _ in range(30):
                try:
                    self._page.wait_for_timeout(1_000)
                except Exception:
                    break
                cur = (self._page.url or "").lower()
                from urllib.parse import urlparse as _urlparse

                host = _urlparse(self._page.url or "").hostname or ""
                if host and host not in redirect_hosts:
                    redirect_hosts.append(host)
                pw_still = None
                try:
                    pw_still = self._find_password_locator()
                except Exception:
                    pw_still = None
                if "/login" not in cur:
                    left_login = True
                    break
                if pw_still is None and "login.opengov.com" not in cur:
                    left_login = True
                    break
                # Error toast on page?
                try:
                    body_txt = self._page.inner_text("body", timeout=2_000) or ""
                except Exception:
                    body_txt = ""
                if re.search(
                    r"invalid\s*(username|password|credentials|email)|incorrect\s*password|"
                    r"login\s*failed|we\s*couldn.?t\s*find|password\s*is\s*incorrect|"
                    r"unable\s*to\s*log\s*in|authentication\s*failed",
                    body_txt,
                    re.I,
                ):
                    result = AuthResult(
                        status=AUTH_FAILED,
                        authenticated=False,
                        message="bad_credentials",
                        details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
                    )
                    record_auth_event(AUTH_FAILED, login_failure=True, failure_reason="bad_credentials")
                    return result
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except Exception:
                pass
            try:
                self._page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        except Exception as exc:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"login_submit_error:{type(exc).__name__}",
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result

        html = self._page.content()
        current = self._page.url or ""
        _track("post_submit", final_url=current[:180], left_login=left_login)
        challenge = self._detect_challenge(html, current)
        if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha|verification", challenge, re.I):
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=challenge,
                challenge_type=challenge,
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=challenge)
            return result

        if re.search(
            r"invalid\s*(username|password|credentials|email)|incorrect\s*password|login\s*failed|"
            r"we\s*couldn.?t\s*find|password\s*is\s*incorrect|unable\s*to\s*log\s*in",
            html,
            re.I,
        ):
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="bad_credentials",
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason="bad_credentials")
            return result

        # Prefer probe on vendor home; also accept authenticated cookies on non-login surfaces
        probe = self._probe_authenticated()
        if probe.get("challenge"):
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=probe.get("challenge_type") or "challenge",
                challenge_type=probe.get("challenge_type"),
                details={"redirect_hosts": redirect_hosts, "flow": flow_stages},
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=result.message)
            return result
        looks = probe.get("authenticated") or self._looks_authenticated(html, current)
        if not looks:
            # Retry probe once after short settle (SPA post-auth)
            try:
                self._page.wait_for_timeout(3_000)
            except Exception:
                pass
            probe = self._probe_authenticated()
            looks = probe.get("authenticated") or self._looks_authenticated(
                self._page.content(), self._page.url or ""
            )
        if looks:
            self._persist_state()
            result = AuthResult(
                status=LOGIN_SUCCESS,
                authenticated=True,
                message="login succeeded",
                details={
                    "redirect_hosts": redirect_hosts,
                    "flow": flow_stages,
                    "email_field_found": True,
                    "password_field_found": True,
                    "real_login_flow": "vendor_login_email_continue_password",
                },
            )
            record_auth_event(LOGIN_SUCCESS, login_success=True)
            record_auth_event(CONNECTED)
            log.info(
                "OpenGov LOGIN_SUCCESS — session persisted hosts=%s",
                ",".join(redirect_hosts),
            )
            return result

        result = AuthResult(
            status=AUTH_FAILED,
            authenticated=False,
            message="post_login_verification_failed",
            details={
                "redirect_hosts": redirect_hosts,
                "flow": flow_stages,
                "final_url": (current or "")[:180],
                "left_login": left_login,
            },
        )
        record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
        return result

    def _iter_search_roots(self) -> list[Any]:
        """Main page + auth iframes (login.opengov.com / Auth0)."""
        assert self._page is not None
        roots: list[Any] = [self._page]
        try:
            for frame in self._page.frames:
                if frame == self._page.main_frame:
                    continue
                fu = (frame.url or "").lower()
                if any(x in fu for x in ("login", "auth", "openid", "okta", "auth0", "authorize")):
                    roots.append(frame)
        except Exception:
            pass
        return roots

    def _find_username_locator(self) -> Any:
        assert self._page is not None
        selectors = (
            'input[name="username"]',
            'input[name="email"]',
            'input#username',
            'input#email',
            'input[type="email"]',
            'input[autocomplete="username"]',
            'input[autocomplete="email"]',
            'input[placeholder*="Email" i]',
            'input[placeholder*="email" i]',
        )
        for root in self._iter_search_roots():
            for sel in selectors:
                loc = root.locator(sel).first
                try:
                    if loc.count() > 0 and loc.is_visible(timeout=1_500):
                        try:
                            if loc.is_disabled():
                                continue
                        except Exception:
                            pass
                        return loc
                except Exception:
                    continue
            for getter in (
                lambda r=root: r.get_by_label(re.compile(r"email|user\s*name|login", re.I)).first,
                lambda r=root: r.get_by_placeholder(re.compile(r"email", re.I)).first,
                lambda r=root: r.get_by_role("textbox", name=re.compile(r"email", re.I)).first,
            ):
                try:
                    loc = getter()
                    if loc.count() > 0 and loc.is_visible(timeout=1_000):
                        return loc
                except Exception:
                    continue
        return None

    def _find_password_locator(self) -> Any:
        assert self._page is not None
        selectors = (
            'input[name="password"]',
            'input#password',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
            'input[placeholder*="Password" i]',
        )
        for root in self._iter_search_roots():
            for sel in selectors:
                loc = root.locator(sel).first
                try:
                    if loc.count() > 0 and loc.is_visible(timeout=1_500):
                        return loc
                except Exception:
                    continue
            for getter in (
                lambda r=root: r.get_by_label(re.compile(r"password", re.I)).first,
                lambda r=root: r.get_by_placeholder(re.compile(r"password", re.I)).first,
                lambda r=root: r.get_by_role("textbox", name=re.compile(r"password", re.I)).first,
            ):
                try:
                    loc = getter()
                    if loc.count() > 0 and loc.is_visible(timeout=1_000):
                        return loc
                except Exception:
                    continue
        return None

    def _submit_login(self) -> bool:
        assert self._page is not None
        page = self._page
        for name in (r"^\s*login\s*$", r"^\s*sign\s*in\s*$", r"^\s*log\s*in\s*$", r"^\s*submit\s*$"):
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.count() > 0 and btn.is_visible(timeout=1_000):
                    btn.click(timeout=10_000)
                    return True
            except Exception:
                continue
        for sel in ('button[type="submit"]', 'input[type="submit"]', 'button:has-text("Login")'):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1_000):
                    loc.click(timeout=10_000)
                    return True
            except Exception:
                continue
        try:
            page.locator('input[type="password"]').first.press("Enter")
            return True
        except Exception:
            return False

    def _persist_state(self) -> None:
        if self._context is None:
            return
        try:
            state = self._context.storage_state()
            save_storage_state(state)
        except Exception as exc:
            log.warning("Failed persisting OpenGov storage state: %s", type(exc).__name__)


def test_connection() -> dict[str, Any]:
    with OpenGovAuthenticatedClient() as client:
        result = client.ensure_authenticated()
        out = result.to_dict()
        out["storage_state_path"] = str(storage_state_path())
        out["storage_state_present"] = load_storage_state() is not None
        out["discovery_scope"] = client.config.discovery_scope
        return out


def is_auth_status_ok(status: str | None) -> bool:
    return status in AUTHENTICATED_STATUSES


def absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)
