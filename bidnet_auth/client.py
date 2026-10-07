"""Reusable BidNet authenticated Playwright client."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bidnet_auth.config import BidNetAuthConfig, load_bidnet_auth_config
from bidnet_auth.session_store import load_storage_state, save_storage_state, storage_state_path
from bidnet_auth.states import (
    AUTH_CHALLENGE,
    AUTH_FAILED,
    AUTHENTICATED_STATUSES,
    CONNECTED,
    DISABLED,
    EXPIRED,
    LOGIN_REQUIRED,
    LOGIN_SUCCESS,
    SESSION_REUSED,
)
from bidnet_auth.telemetry import record_auth_event

log = logging.getLogger("govtracker.bidnet_auth.client")

_CHALLENGE_PATTERNS = re.compile(
    r"(captcha|recaptcha|hcaptcha|g-recaptcha|cf-challenge|"
    r"two[\-\s]?factor|multi[\-\s]?factor|\bmfa\b|one[\-\s]?time\s*code|"
    r"verification\s*code|email\s*verification|verify\s*your\s*(email|identity)|"
    r"unusual\s*activity|human\s*verification|bot\s*detection|"
    r"are\s*you\s*a\s*robot|security\s*check)",
    re.I,
)

_AUTH_POSITIVE = re.compile(
    r"(log\s*out|sign\s*out|my\s*account|my\s*profile|supplier\s*home|"
    r"account\s*settings|welcome,\s*|private/supplier)",
    re.I,
)

_AUTH_NEGATIVE = re.compile(
    r"(public/authentication/login|sign\s*in\s*to\s*continue|"
    r"please\s*log\s*in|invalid\s*(username|password|credentials)|"
    r"forgot\s*your\s*password)",
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


class BidNetAuthenticatedClient:
    """Launch Chromium, reuse/login BidNet session, fetch authenticated pages."""

    def __init__(self, config: BidNetAuthConfig | None = None) -> None:
        self.config = config or load_bidnet_auth_config()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.last_result: AuthResult | None = None

    def __enter__(self) -> "BidNetAuthenticatedClient":
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
        """Validate stored session or log in. Persists refreshed state on success."""
        cfg = self.config
        if not cfg.auth_enabled:
            result = AuthResult(status=DISABLED, authenticated=False, message="BIDNET_AUTH_ENABLED=false")
            self.last_result = result
            record_auth_event(DISABLED, failure_reason="disabled")
            return result
        if not cfg.credentials_present:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="BIDNET_USERNAME/BIDNET_PASSWORD not configured",
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
            log.warning("BidNet browser unavailable: %s", type(exc).__name__)
            return result

        # 1) Try existing session
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
            log.info("BidNet stored session expired — automatic login required")
            record_auth_event(EXPIRED, failure_reason="session_expired")

        # 2) Automatic login
        login = self._perform_login()
        self.last_result = login
        return login

    def fetch_html(self, url: str, *, timeout_ms: int = 90_000, prefer_http: bool = True) -> str:
        """Fetch HTML. Prefer authenticated HTTP request; fall back to page navigation."""
        if not self.is_authenticated:
            raise RuntimeError("BidNet client is not authenticated")
        if prefer_http and self._context is not None:
            try:
                resp = self._context.request.get(url, timeout=timeout_ms)
                if resp.ok:
                    html = resp.text()
                    final = str(getattr(resp, "url", None) or url)
                    challenge = self._detect_challenge(html, final)
                    if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha", challenge, re.I):
                        raise RuntimeError(f"AUTH_CHALLENGE:{challenge}")
                    # Enough solicitation content → skip slow page.goto/networkidle
                    low = (html or "").lower()
                    if any(
                        marker in low
                        for marker in (
                            "mets-field",
                            "issuing organization",
                            "closing date",
                            "solicitation number",
                            "ai-public-overview",
                        )
                    ):
                        return html
            except RuntimeError:
                raise
            except Exception as exc:
                log.debug("BidNet HTTP fetch fallback to page: %s", type(exc).__name__)

        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            self._page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        # Wait for abstract / solicitation content markers (member view)
        for sel in (
            ".mets-field-body",
            "h1",
            "#ai-public-overview-content",
            "text=Issuing Organization",
            "text=Closing Date",
            "text=Solicitation Number",
        ):
            try:
                self._page.wait_for_selector(sel, timeout=5_000)
                break
            except Exception:
                continue
        html = self._page.content()
        challenge = self._detect_challenge(html, self._page.url)
        if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha", challenge, re.I):
            raise RuntimeError(f"AUTH_CHALLENGE:{challenge}")
        return html

    def download_bytes(self, url: str, *, timeout_ms: int = 90_000) -> bytes | None:
        if not self.is_authenticated or self._context is None:
            return None

        def _is_html(body: bytes | None) -> bool:
            if not body or len(body) < 5:
                return False
            try:
                from bidnet_engine.package_materialization import looks_like_html_bytes

                return looks_like_html_bytes(body)
            except Exception:
                head = body.lstrip()[:64].lower()
                return head.startswith((b"<!doctype", b"<html", b"<head"))

        # 1) API-style fetch with attachment-friendly Accept
        try:
            resp = self._context.request.get(
                url,
                timeout=timeout_ms,
                headers={
                    "Accept": (
                        "application/pdf,application/vnd.ms-excel,"
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                        "application/octet-stream,*/*"
                    ),
                },
            )
            if resp.ok:
                body = resp.body()
                ctype = str((resp.headers or {}).get("content-type") or "").lower()
                if body and not _is_html(body) and "text/html" not in ctype:
                    return body
                api_html = body if body and (_is_html(body) or "text/html" in ctype) else None
            else:
                api_html = None
        except Exception as exc:
            log.warning("BidNet authenticated download failed: %s", type(exc).__name__)
            api_html = None

        # 2) Browser download: many BidNet attachment URLs only emit bytes via navigation
        if self._page is not None:
            try:
                with self._page.expect_download(timeout=min(timeout_ms, 90_000)) as dl_info:
                    self._page.goto(url, wait_until="commit", timeout=timeout_ms)
                download = dl_info.value
                path = download.path()
                if path:
                    data = open(path, "rb").read()
                    if data and not _is_html(data):
                        return data
            except Exception as exc:
                log.info("BidNet browser download fallback: %s", type(exc).__name__)
            # 3) If navigation landed on HTML, return it so caller can harvest links
            try:
                content = self._page.content()
                if content and len(content) > 64:
                    raw = content.encode("utf-8", errors="ignore")
                    if api_html is None:
                        return raw
                    # Prefer longer HTML (more link harvest surface)
                    return raw if len(raw) >= len(api_html or b"") else api_html
            except Exception:
                pass

        return api_html

    def discover_attachment_links(self, detail_url: str, *, timeout_ms: int = 75_000) -> list[dict[str, Any]]:
        """DOM-scrape BidNet detail/documents UI for real attachment hrefs (not viewer HTML)."""
        if not self.is_authenticated or self._page is None or not detail_url:
            return []
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates: list[str] = []
        try:
            from bidnet_engine.package_materialization import (
                resolve_bidnet_private_detail_url_candidates,
            )

            candidates = resolve_bidnet_private_detail_url_candidates(detail_url)
        except Exception:
            candidates = []
        if detail_url and detail_url not in candidates:
            candidates.append(detail_url)

        landed = False
        try:
            for navigate_url in candidates:
                self.fetch_html(navigate_url, timeout_ms=timeout_ms)
                title = ""
                final_u = ""
                try:
                    title = (self._page.title() or "").lower()
                    final_u = (self._page.url or "").lower()
                except Exception:
                    pass
                if "search" in title or "welcome" in title or "/search" in final_u:
                    continue
                landed = True
                break
            if not landed and candidates:
                # Last attempt — keep whatever we have for diag scrape
                self.fetch_html(candidates[0], timeout_ms=timeout_ms)
            # Persist page HTML for diagnostics
            try:
                from m3_data_root import data_path
                import hashlib as _hl

                html_now = self._page.content() or ""
                if html_now:
                    diag = data_path("bidnet_auth", "html_diag")
                    diag.mkdir(parents=True, exist_ok=True)
                    h = _hl.sha1((detail_url or "").encode()).hexdigest()[:12]
                    (diag / f"{h}_discover.html").write_text(html_now[:80_000], encoding="utf-8")
            except Exception:
                pass
        except Exception as exc:
            log.warning("discover_attachment_links navigate failed: %s", type(exc).__name__)
            return []

        # Open Documents / Attachments tab when present
        for label in (
            r"Documents",
            r"Attachments",
            r"Bid\s*Documents",
            r"Files",
            r"Solicitation\s*Documents",
        ):
            try:
                tab = self._page.get_by_role("tab", name=re.compile(label, re.I))
                if tab.count() > 0:
                    tab.first.click(timeout=3_000)
                    self._page.wait_for_timeout(800)
                    break
            except Exception:
                pass
            try:
                link = self._page.get_by_role("link", name=re.compile(label, re.I))
                if link.count() > 0:
                    link.first.click(timeout=3_000)
                    self._page.wait_for_timeout(800)
                    break
            except Exception:
                pass

        def _add(href: str, name: str) -> None:
            href = (href or "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                return
            low = href.lower()
            if any(x in low for x in ("login", "logout", "register", "captcha", "authentication")):
                return
            if href.startswith("/"):
                try:
                    from urllib.parse import urljoin

                    href = urljoin(self._page.url or detail_url, href)
                except Exception:
                    return
            if not href.startswith("http") or href in seen:
                return
            # Prefer file-like or download endpoints
            if not re.search(
                r"\.(pdf|docx?|xlsx?|csv|zip)(?:$|\?)|download|attachment|document|fileId|docId|getFile|solicitation",
                href,
                re.I,
            ):
                # Keep named document links even without extension
                if not re.search(r"\.(pdf|docx?|xlsx?|csv|zip)\b", name or "", re.I):
                    if not re.search(r"addend|amend|attach|exhibit|schedule|spec|bid\s*form|pricing", name or "", re.I):
                        return
            seen.add(href)
            found.append(
                {
                    "document_name": (name or href.rsplit("/", 1)[-1] or "attachment")[:160],
                    "document_url": href,
                    "filename": (name or href.rsplit("/", 1)[-1] or "attachment")[:160],
                    "url": href,
                    "source_url": href,
                    "retrieval_status": "URL_DISCOVERED_FROM_PAGE",
                    "requires_auth": True,
                    "page_discovered": True,
                }
            )

        try:
            anchors = self._page.locator("a[href]").all()
            for a in anchors[:250]:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.inner_text() or "").strip()
                    title = a.get_attribute("title") or ""
                    _add(href, text or title)
                except Exception:
                    continue
        except Exception as exc:
            log.info("discover_attachment_links anchor scan: %s", type(exc).__name__)

        # data-download / button hooks
        try:
            for sel in (
                "[data-download-url]",
                "[data-file-url]",
                "[data-document-url]",
                "a[download]",
            ):
                for el in self._page.locator(sel).all()[:40]:
                    try:
                        href = (
                            el.get_attribute("data-download-url")
                            or el.get_attribute("data-file-url")
                            or el.get_attribute("data-document-url")
                            or el.get_attribute("href")
                            or ""
                        )
                        name = (el.inner_text() or el.get_attribute("download") or "").strip()
                        _add(href, name)
                    except Exception:
                        continue
        except Exception:
            pass

        return found[:30]

    def navigate_and_collect_json(self, url: str, *, timeout_ms: int = 90_000) -> tuple[str, list[Any]]:
        """Navigate URL and capture JSON XHR/fetch payloads for structured harvest."""
        if not self.is_authenticated:
            raise RuntimeError("BidNet client is not authenticated")
        assert self._page is not None
        captured: list[Any] = []

        def _on_response(response: Any) -> None:
            try:
                ctype = (response.headers or {}).get("content-type", "")
                u = response.url or ""
                if response.status != 200:
                    return
                if "application/json" not in ctype and "/api/" not in u.lower():
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
            "viewport": {"width": 1280, "height": 900},
        }
        if state:
            kwargs["storage_state"] = state
        self._context = self._browser.new_context(**kwargs)
        self._page = self._context.new_page()

    def _probe_authenticated(self) -> dict[str, Any]:
        assert self._page is not None
        url = self.config.verify_url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
        except Exception as exc:
            return {"authenticated": False, "error": type(exc).__name__}

        html = self._page.content()
        current = self._page.url or ""
        challenge = self._detect_challenge(html, current)
        if challenge:
            return {"authenticated": False, "challenge": True, "challenge_type": challenge}

        if self._looks_authenticated(html, current):
            return {"authenticated": True}
        return {"authenticated": False}

    def _looks_authenticated(self, html: str, url: str) -> bool:
        if "public/authentication/login" in (url or "").lower():
            return False
        if "/private/" in (url or "").lower() and "login" not in (url or "").lower():
            return True
        # Account markers on page
        if self._page is not None:
            try:
                if self._page.locator('a[href*="logout"], a[href*="log-out"], a[href*="signout"]').count() > 0:
                    return True
                if self._page.get_by_role("link", name=re.compile(r"log\s*out|sign\s*out", re.I)).count() > 0:
                    return True
            except Exception:
                pass
        if _AUTH_POSITIVE.search(html or "") and not _AUTH_NEGATIVE.search(html or ""):
            return True
        # Login form still present ⇒ not authenticated
        if re.search(r'type=["\']password["\']', html or "", re.I) and "authentication/login" in (url or "").lower():
            return False
        return False

    def _detect_challenge(self, html: str, url: str) -> str | None:
        blob = f"{url or ''}\n{html or ''}"
        m = _CHALLENGE_PATTERNS.search(blob)
        if not m:
            # iframe captcha widgets
            if re.search(r"<iframe[^>]+(recaptcha|hcaptcha|captcha)", html or "", re.I):
                return "captcha_iframe"
            return None
        return m.group(0).lower()[:80]

    def _perform_login(self) -> AuthResult:
        assert self._page is not None
        cfg = self.config
        record_auth_event(LOGIN_REQUIRED, login_attempt=True)
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

        html = self._page.content()
        challenge = self._detect_challenge(html, self._page.url)
        if challenge:
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=challenge,
                challenge_type=challenge,
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=challenge)
            log.warning("BidNet AUTH_CHALLENGE during login: %s", challenge)
            return result

        user_sel = self._find_username_locator()
        pass_sel = self._find_password_locator()
        if user_sel is None or pass_sel is None:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="login_fields_not_found",
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result

        try:
            user_sel.fill(cfg.username, timeout=15_000)
            pass_sel.fill(cfg.password, timeout=15_000)
            submitted = self._submit_login()
            if not submitted:
                result = AuthResult(
                    status=AUTH_FAILED,
                    authenticated=False,
                    message="login_submit_not_found",
                )
                record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
                return result
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=45_000)
            except Exception:
                pass
            try:
                self._page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
        except Exception as exc:
            # Never include exception text that might echo credentials
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message=f"login_submit_error:{type(exc).__name__}",
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result

        html = self._page.content()
        current = self._page.url or ""
        challenge = self._detect_challenge(html, current)
        if challenge:
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=challenge,
                challenge_type=challenge,
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=challenge)
            log.warning("BidNet AUTH_CHALLENGE after login submit: %s", challenge)
            return result

        # Bad credentials markers
        if re.search(r"invalid\s*(username|password|credentials)|incorrect\s*password|login\s*failed", html, re.I):
            result = AuthResult(status=AUTH_FAILED, authenticated=False, message="bad_credentials")
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason="bad_credentials")
            return result

        # Verify on authenticated surface
        probe = self._probe_authenticated()
        if probe.get("challenge"):
            result = AuthResult(
                status=AUTH_CHALLENGE,
                authenticated=False,
                message=probe.get("challenge_type") or "challenge",
                challenge_type=probe.get("challenge_type"),
            )
            record_auth_event(AUTH_CHALLENGE, challenge=True, failure_reason=result.message)
            return result
        if probe.get("authenticated") or self._looks_authenticated(html, current):
            self._persist_state()
            result = AuthResult(
                status=LOGIN_SUCCESS,
                authenticated=True,
                message="login succeeded",
            )
            # Also mark CONNECTED for UI
            record_auth_event(LOGIN_SUCCESS, login_success=True)
            record_auth_event(CONNECTED, login_success=False)
            log.info("BidNet LOGIN_SUCCESS — session persisted")
            return result

        result = AuthResult(
            status=AUTH_FAILED,
            authenticated=False,
            message="post_login_verification_failed",
        )
        record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
        return result

    def _find_username_locator(self) -> Any:
        assert self._page is not None
        page = self._page
        candidates = [
            'input[name="username"]',
            'input#username',
            'input[autocomplete="username"]',
            'input[type="email"]',
            'input[name="email"]',
            'input[id*="user" i]',
            'input[name*="user" i]',
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible(timeout=1_500):
                    return loc
            except Exception:
                continue
        try:
            loc = page.get_by_label(re.compile(r"user\s*name|email|login", re.I)).first
            if loc.count() > 0:
                return loc
        except Exception:
            pass
        return None

    def _find_password_locator(self) -> Any:
        assert self._page is not None
        page = self._page
        candidates = [
            'input[name="password"]',
            'input#password',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible(timeout=1_500):
                    return loc
            except Exception:
                continue
        try:
            loc = page.get_by_label(re.compile(r"password", re.I)).first
            if loc.count() > 0:
                return loc
        except Exception:
            pass
        return None

    def _submit_login(self) -> bool:
        assert self._page is not None
        page = self._page
        # Prefer explicit login button
        for name in (r"^log\s*in$", r"^sign\s*in$", r"^login$", r"^submit$"):
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I)).first
                if btn.count() > 0 and btn.is_visible(timeout=1_000):
                    btn.click(timeout=10_000)
                    return True
            except Exception:
                continue
        for sel in (
            'button[type="submit"]',
            'input[type="submit"]',
            'button[name*="login" i]',
            'input[value*="Log" i]',
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1_000):
                    loc.click(timeout=10_000)
                    return True
            except Exception:
                continue
        # Fallback: press Enter in password field
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
            log.warning("Failed persisting BidNet storage state: %s", type(exc).__name__)


def test_connection() -> dict[str, Any]:
    """Owner UI / ops: attempt session reuse or login once."""
    with BidNetAuthenticatedClient() as client:
        result = client.ensure_authenticated()
        out = result.to_dict()
        out["storage_state_path"] = str(storage_state_path())
        out["storage_state_present"] = load_storage_state() is not None
        # Never echo credentials
        out.pop("username", None)
        out.pop("password", None)
        return out


def is_auth_status_ok(status: str | None) -> bool:
    return status in AUTHENTICATED_STATUSES
