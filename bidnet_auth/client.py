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

        # Internal repair: allow SESSION REUSE without credentials.
        # Credentials are required only for fresh login / refresh.
        has_session = load_storage_state() is not None
        if not cfg.credentials_present and not has_session:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED: BIDNET_USERNAME/BIDNET_PASSWORD not configured and no stored session",
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

        # 1) Try existing session (works even when credentials absent)
        if has_session:
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
            if not cfg.credentials_present:
                result = AuthResult(
                    status=EXPIRED,
                    authenticated=False,
                    message="EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED: stored session expired and no credentials for refresh",
                )
                self.last_result = result
                record_auth_event(AUTH_FAILED, failure_reason=result.message, login_failure=True)
                return result

        # 2) Automatic login (requires credentials)
        if not cfg.credentials_present:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED: BIDNET_USERNAME/BIDNET_PASSWORD not configured",
            )
            self.last_result = result
            record_auth_event(AUTH_FAILED, failure_reason=result.message, login_failure=True)
            return result
        login = self._perform_login()
        self.last_result = login
        return login

    def fetch_html(self, url: str, *, timeout_ms: int = 20_000, prefer_http: bool = True) -> str:
        """Fetch HTML. Prefer authenticated HTTP request; fall back to page navigation.

        Defaults are capped so a 45s per-opportunity budget remains feasible.
        Never waits on networkidle longer than 3s.
        """
        if not self.is_authenticated:
            raise RuntimeError("BidNet client is not authenticated")
        timeout_ms = max(1_000, min(int(timeout_ms), 45_000))
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
                    # Any substantial HTML response is good enough — do not fall through
                    # to Playwright navigation (was the main unbounded hang source).
                    if html and len(html) >= 800:
                        return html
            except RuntimeError:
                raise
            except Exception as exc:
                log.debug("BidNet HTTP fetch fallback to page: %s", type(exc).__name__)

        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            self._page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass
        # One short content-marker wait only (was up to 5 × 5s)
        for sel in (
            ".mets-field-body",
            "#ai-public-overview-content",
            "text=Solicitation Number",
        ):
            try:
                self._page.wait_for_selector(sel, timeout=2_000)
                break
            except Exception:
                continue
        html = self._page.content()
        challenge = self._detect_challenge(html, self._page.url)
        if challenge and re.search(r"captcha|mfa|two.?factor|recaptcha", challenge, re.I):
            raise RuntimeError(f"AUTH_CHALLENGE:{challenge}")
        return html

    def download_bytes(self, url: str, *, timeout_ms: int = 25_000) -> bytes | None:
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
                with self._page.expect_download(timeout=min(timeout_ms, 25_000)) as dl_info:
                    self._page.goto(url, wait_until="commit", timeout=min(timeout_ms, 25_000))
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

    def discover_attachment_links(self, detail_url: str, *, timeout_ms: int = 20_000) -> list[dict[str, Any]]:
        """DOM-scrape BidNet detail/documents UI for real attachment hrefs (not viewer HTML)."""
        if not self.is_authenticated or self._page is None or not detail_url:
            return []
        from pathlib import Path

        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates: list[str] = []
        # Prefer private abstract — /documents and ?innerTabId=docs-items cold URLs 404.
        # Documents tab is AJAX-loaded via click on a[href*='innerTabId=docs-items'].
        sid_m = re.search(r"/private/solicitations/(\d{6,})", detail_url or "", re.I)
        if not sid_m:
            sid_m = re.search(r"/private/supplier/solicitations/(\d{6,})", detail_url or "", re.I)
        if sid_m:
            sid = sid_m.group(1)
            candidates.append(
                f"https://www.bidnetdirect.com/private/solicitations/{sid}/abstract?target=view"
            )
        try:
            from bidnet_engine.package_materialization import (
                resolve_bidnet_private_detail_url_candidates,
            )

            for u in resolve_bidnet_private_detail_url_candidates(detail_url) or []:
                if u and u not in candidates:
                    # Skip known-broken document path variants for private abstracts
                    if re.search(r"/documents(?:$|\?)", u, re.I):
                        continue
                    if "innerTabId=docs-items" in u:
                        continue
                    candidates.append(u)
        except Exception:
            pass
        if detail_url and detail_url not in candidates:
            # Strip docs-items query — cold navigation 404s; open abstract instead
            cleaned = re.sub(r"([?&])innerTabId=docs-items&?", r"\1", detail_url).rstrip("?&")
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

        landed = False
        network_urls: list[str] = []

        def _on_response(response: Any) -> None:
            try:
                u = response.url or ""
                ctype = str((response.headers or {}).get("content-type") or "").lower()
                if response.status != 200:
                    return
                low = u.lower()
                # Reject static assets / JS interceptors masquerading as "intercept"
                if any(
                    x in low
                    for x in (
                        "/jawr/",
                        "interceptor.js",
                        ".js?",
                        ".css",
                        "/solicitations/search",
                        "subscription-management",
                    )
                ):
                    return
                if any(
                    x in low
                    for x in (
                        "download/intercept",
                        "getfile",
                        "attachment",
                        "fileid",
                        "docid",
                        ".pdf",
                        ".xlsx",
                        ".xls",
                        ".docx",
                        ".zip",
                        ".csv",
                    )
                ) or any(x in ctype for x in ("pdf", "sheet", "zip", "msword", "octet-stream")):
                    network_urls.append(u)
            except Exception:
                pass

        try:
            self._page.on("response", _on_response)
            for navigate_url in candidates:
                # Must use browser navigation — Documents tab is AJAX DOM, not HTTP HTML.
                self.fetch_html(navigate_url, timeout_ms=timeout_ms, prefer_http=False)
                title = ""
                final_u = ""
                try:
                    title = (self._page.title() or "").lower()
                    final_u = (self._page.url or "").lower()
                except Exception:
                    pass
                if "search" in title or "welcome" in title or "/search" in final_u:
                    continue
                if "404" in title or "cannot be found" in (self._page.content() or "")[:800].lower():
                    continue
                landed = True
                break
            if not landed and candidates:
                # Last attempt — keep whatever we have for diag scrape
                self.fetch_html(candidates[0], timeout_ms=timeout_ms, prefer_http=False)
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
            try:
                self._page.remove_listener("response", _on_response)
            except Exception:
                pass
            return []

        # Open Documents tab — BidNet injects rows via /abstract/docs-items AJAX.
        # Prefer the exact docs-items tab href; role=tab does not match this UI.
        docs_tab_opened = False
        try:
            tab_loc = self._page.locator("a[href*='innerTabId=docs-items']")
            if tab_loc.count() > 0:
                tab_loc.first.click(timeout=5_000)
                docs_tab_opened = True
                try:
                    self._page.wait_for_selector(
                        "table.solicitationDocumentsTable a[href*='attachment-download'], "
                        "a[id^='attachmentDownloadLnk_']",
                        timeout=min(12_000, timeout_ms),
                    )
                except Exception:
                    self._page.wait_for_timeout(2_000)
        except Exception:
            pass
        if not docs_tab_opened:
            for label in (
                r"Documents\s*\(\s*\d+\s*\)",
                r"^Documents$",
                r"Attachments",
                r"Bid\s*Documents",
                r"Solicitation\s*Documents",
            ):
                try:
                    link = self._page.get_by_role("link", name=re.compile(label, re.I))
                    if link.count() > 0:
                        link.first.click(timeout=3_000)
                        docs_tab_opened = True
                        self._page.wait_for_timeout(2_000)
                        break
                except Exception:
                    pass

        def _add(href: str, name: str, *, local_path: str | None = None) -> None:
            href = (href or "").strip()
            if local_path:
                key = f"local:{local_path}"
                if key in seen:
                    return
                seen.add(key)
                # Upgrade existing URL-only entry for same attachment
                att_id = None
                m_att = re.search(r"docs-items/(\d+)/attachment-download", href or "", re.I)
                if m_att:
                    att_id = m_att.group(1)
                    seen.add(f"att:{att_id}")
                upgraded = False
                if att_id or href:
                    for existing in found:
                        same = (
                            (att_id and existing.get("attachment_id") == att_id)
                            or (href and existing.get("document_url") == href)
                            or (
                                name
                                and existing.get("document_name") == name
                                and not existing.get("local_path")
                            )
                        )
                        if same and not existing.get("local_path"):
                            existing.update(
                                {
                                    "local_path": local_path,
                                    "retrieval_status": "BROWSER_DOWNLOAD",
                                    "document_url": href or existing.get("document_url"),
                                    "url": href or existing.get("url"),
                                    "source_url": href or existing.get("source_url"),
                                }
                            )
                            upgraded = True
                            break
                if upgraded:
                    return
                found.append(
                    {
                        "document_name": (name or Path(local_path).name)[:160],
                        "document_url": href or f"file://{local_path}",
                        "filename": (name or Path(local_path).name)[:160],
                        "url": href or f"file://{local_path}",
                        "source_url": href or f"file://{local_path}",
                        "local_path": local_path,
                        "attachment_id": att_id,
                        "retrieval_status": "BROWSER_DOWNLOAD",
                        "requires_auth": True,
                        "page_discovered": True,
                    }
                )
                return
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
            if not href.startswith("http"):
                return
            # Reject static JS/CSS/search pages (interceptor.js previously matched "intercept")
            if re.search(
                r"\.(js|css|map|png|jpe?g|gif|svg|woff2?)(?:$|\?)|/jawr/|interceptor\.js|"
                r"/solicitations/search|subscription-management",
                href,
                re.I,
            ):
                return
            # Prefer file-like or download endpoints (incl. BidNet docs-items paths)
            if not re.search(
                r"\.(pdf|docx?|xlsx?|xls|csv|zip)(?:$|\?)|download/intercept|attachment-download|"
                r"attachment|fileId|docId|getFile|docs-items/\d+",
                href,
                re.I,
            ):
                # Keep named document links even without extension
                if not re.search(r"\.(pdf|docx?|xlsx?|xls|csv|zip)\b", name or "", re.I):
                    if not re.search(r"addend|amend|attach|exhibit|schedule|spec|bid\s*form|pricing", name or "", re.I):
                        return
            # Extract BidNet attachment id when present
            att_id = None
            m_att = re.search(r"docs-items/(\d+)/attachment-download", href, re.I)
            if m_att:
                att_id = m_att.group(1)
            # Dedupe by attachment id or URL — allow local_path upgrade later
            dedupe_key = f"att:{att_id}" if att_id else href
            if dedupe_key in seen or href in seen:
                return
            seen.add(dedupe_key)
            seen.add(href)
            found.append(
                {
                    "document_name": (name or href.rsplit("/", 1)[-1] or "attachment")[:160],
                    "document_url": href,
                    "filename": (name or href.rsplit("/", 1)[-1] or "attachment")[:160],
                    "url": href,
                    "source_url": href,
                    "attachment_id": att_id,
                    "retrieval_status": "URL_DISCOVERED_FROM_PAGE",
                    "requires_auth": True,
                    "page_discovered": True,
                    "docs_tab": True,
                }
            )

        # Primary harvest: solicitationDocumentsTable attachment-download links
        try:
            for a in self._page.locator(
                "table.solicitationDocumentsTable a[href*='attachment-download'], "
                "a[id^='attachmentDownloadLnk_']"
            ).all()[:40]:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.inner_text() or "").strip()
                    _add(href, text)
                except Exception:
                    continue
        except Exception as exc:
            log.info("discover_attachment_links docs-table scan: %s", type(exc).__name__)

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

        # data-download / button hooks + onclick URL harvest
        try:
            for sel in (
                "[data-download-url]",
                "[data-file-url]",
                "[data-document-url]",
                "a[download]",
                "button[onclick*='download' i]",
                "a[onclick*='download' i]",
                "[href*='download/intercept']",
                "[href*='getFile']",
            ):
                for el in self._page.locator(sel).all()[:50]:
                    try:
                        href = (
                            el.get_attribute("data-download-url")
                            or el.get_attribute("data-file-url")
                            or el.get_attribute("data-document-url")
                            or el.get_attribute("href")
                            or ""
                        )
                        onclick = el.get_attribute("onclick") or ""
                        if not href and onclick:
                            m = re.search(r"https?://[^\"'\s]+", onclick)
                            if m:
                                href = m.group(0)
                        name = (el.inner_text() or el.get_attribute("download") or "").strip()
                        _add(href, name)
                    except Exception:
                        continue
        except Exception:
            pass

        # Network-captured file URLs from Documents tab / XHR
        for u in network_urls:
            _add(u, u.rsplit("/", 1)[-1][:80])

        # Always attempt browser downloads. BidNet often exposes viewer/detail hrefs that
        # populate `found` without yielding real schedule binaries — Download All / row
        # clicks are required for package ZIPs and pricing sheets.
        browser_dl_hits = 0
        try:
            from m3_data_root import data_path
            import hashlib as _hl
            import shutil

            out_dir = data_path("bidnet_auth", "documents", "_browser_dl")
            out_dir.mkdir(parents=True, exist_ok=True)

            def _click_download(el: Any, *, timeout_ms: int = 25_000) -> bool:
                nonlocal browser_dl_hits
                try:
                    with self._page.expect_download(timeout=timeout_ms) as dl_info:
                        el.click(timeout=3_000)
                    download = dl_info.value
                    src = download.path()
                    suggested = download.suggested_filename or "bidnet_doc.bin"
                    if not src:
                        return False
                    raw = Path(src).read_bytes()
                    if len(raw) < 64:
                        return False
                    # Skip HTML masquerading as downloads
                    head = raw[:200].lstrip().lower()
                    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
                        return False
                    h = _hl.sha256(raw).hexdigest()[:16]
                    dest = out_dir / f"{h}_{re.sub(r'[^a-zA-Z0-9._-]+', '_', suggested)[:80]}"
                    shutil.copyfile(src, dest)
                    _add(download.url or "", suggested, local_path=str(dest))
                    browser_dl_hits += 1
                    return True
                except Exception:
                    return False

            # 1) Download All / package export first
            for role, pattern in (
                ("button", r"download\s*all|download\s*package|export\s*all|zip"),
                ("link", r"download\s*all|download\s*package|export\s*all"),
            ):
                try:
                    loc = self._page.get_by_role(role, name=re.compile(pattern, re.I))
                    for el in loc.all()[:3]:
                        if _click_download(el, timeout_ms=45_000):
                            break
                except Exception:
                    pass

            # 2) Schedule-like named links/buttons (even when generic hrefs already found)
            schedule_pat = re.compile(
                r"(\.xlsx|\.xls|\.csv|\.zip|price|pricing|bid\s*form|bid\s*schedule|"
                r"item\s*list|line\s*item|bom|equipment|material|spec(?:ification)?|"
                r"schedule|proposal\s*form|cost\s*sheet|unit\s*price)",
                re.I,
            )
            for role in ("link", "button"):
                try:
                    loc = self._page.get_by_role(role, name=schedule_pat)
                    for el in loc.all()[:10]:
                        _click_download(el)
                        if browser_dl_hits >= 8:
                            break
                except Exception:
                    pass

            # 3) Generic download controls if still thin
            if browser_dl_hits < 2:
                try:
                    dl_btns = self._page.get_by_role(
                        "button",
                        name=re.compile(r"download|save|export", re.I),
                    )
                    for el in dl_btns.all()[:8]:
                        _click_download(el)
                        if browser_dl_hits >= 6:
                            break
                except Exception:
                    pass
                try:
                    link_btns = self._page.get_by_role(
                        "link",
                        name=re.compile(r"download|\.pdf|\.xlsx|addendum|attachment", re.I),
                    )
                    for el in link_btns.all()[:10]:
                        _click_download(el)
                        if browser_dl_hits >= 8:
                            break
                except Exception:
                    pass

            # 4) BidNet docs-items attachment-download rows (proven path)
            try:
                att_links = self._page.locator(
                    "table.solicitationDocumentsTable a[href*='attachment-download'], "
                    "a[id^='attachmentDownloadLnk_']"
                )
                for el in att_links.all()[:20]:
                    _click_download(el, timeout_ms=30_000)
                    if browser_dl_hits >= 12:
                        break
            except Exception:
                pass

            # 5) Document grid / table download icons
            if browser_dl_hits < 2:
                try:
                    for sel in (
                        "table a[href*='download']",
                        "table a[href*='intercept']",
                        "table a[href*='getFile']",
                        "[class*='document'] a[href*='download']",
                        "a[title*='Download' i]",
                        "button[title*='Download' i]",
                    ):
                        for el in self._page.locator(sel).all()[:12]:
                            _click_download(el)
                            if browser_dl_hits >= 8:
                                break
                        if browser_dl_hits >= 8:
                            break
                except Exception:
                    pass
        except Exception as exc:
            log.info("discover_attachment_links browser download: %s", type(exc).__name__)

        try:
            self._page.remove_listener("response", _on_response)
        except Exception:
            pass

        # Prefer browser-captured binaries first for materialization
        found.sort(
            key=lambda d: (
                0 if d.get("local_path") else 1,
                0
                if re.search(
                    r"pric|schedule|bid|item|bom|equip|material|spec|\.xlsx|\.xls|\.zip",
                    str(d.get("filename") or d.get("document_name") or ""),
                    re.I,
                )
                else 1,
                str(d.get("filename") or ""),
            )
        )
        return found[:40]

    def resolve_detail_via_authenticated_search(
        self,
        *,
        title: str,
        source_url: str | None = None,
        search_html: str | None = None,
        timeout_ms: int = 20_000,
    ) -> dict[str, Any]:
        """Navigate BidNet private search → interception gateway → solicitation detail.

        Root-cause repair: authenticated GETs of statewide/open-bids sources land on
        `/private/supplier/solicitations/search`. Result rows use
        `/private/supplier/interception/open-solicitation/{id}?target=view`
        (class solicitationsTitleLink), not direct /private/supplier/solicitations/{id}/view.
        """
        from urllib.parse import quote

        from bidnet_engine.package_materialization import (
            extract_search_result_interception_links,
            is_bidnet_search_page_url,
            is_bidnet_solicitation_detail_url,
        )

        if not self.is_authenticated or self._page is None:
            return {"ok": False, "status": "NOT_AUTHENTICATED"}

        q = re.sub(r"\s+", " ", (title or "").strip())[:120]
        timeout_ms = max(1_000, min(int(timeout_ms), 45_000))
        html = search_html or ""

        # If caller already has search HTML, parse it first (no extra navigation)
        candidates = extract_search_result_interception_links(html, title_hint=q) if html else []

        if not candidates and q:
            search_urls = [
                f"https://www.bidnetdirect.com/private/supplier/solicitations/search?target=init&keywords={quote(q)}",
                f"https://www.bidnetdirect.com/private/supplier/solicitations/search?keywords={quote(q)}",
            ]
            for su in search_urls:
                try:
                    html = self.fetch_html(su, timeout_ms=timeout_ms, prefer_http=False) or ""
                    # Prefer page content after JS (title links)
                    try:
                        html = self._page.content() or html
                    except Exception:
                        pass
                    candidates = extract_search_result_interception_links(html, title_hint=q)
                    if candidates:
                        break
                    # Keyword box + Search button (textarea#solicitationSingleBoxSearch)
                    try:
                        box = self._page.locator("#solicitationSingleBoxSearch")
                        if box.count():
                            box.first.fill(q, timeout=3_000)
                            btn = self._page.locator("#topSearchButton")
                            if btn.count():
                                btn.first.click(timeout=3_000)
                            else:
                                box.first.press("Enter")
                            self._page.wait_for_timeout(1_500)
                            html = self._page.content() or html
                            candidates = extract_search_result_interception_links(html, title_hint=q)
                            if candidates:
                                break
                    except Exception:
                        pass
                except Exception as exc:
                    log.debug("search navigate failed: %s", type(exc).__name__)
                    continue

        if not candidates:
            return {
                "ok": False,
                "status": "NO_INTERCEPTION_RESULT",
                "source_url": (source_url or "")[:160] or None,
                "query": q or None,
            }

        # Prefer open-solicitation over view-notice
        ordered = sorted(
            candidates,
            key=lambda r: (0 if r.get("kind") == "open_solicitation" else 1),
        )
        last_err = None
        for cand in ordered[:4]:
            href = str(cand.get("href") or "")
            if not href:
                continue
            try:
                self._page.goto(href, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=3_000)
                except Exception:
                    pass
                try:
                    self._page.wait_for_timeout(600)
                except Exception:
                    pass
                final = str(self._page.url or "")
                detail_html = self._page.content() or ""
                if is_bidnet_search_page_url(final):
                    last_err = "STILL_SEARCH"
                    continue
                if not (
                    is_bidnet_solicitation_detail_url(final)
                    or re.search(r"/private/solicitations/\d+", final, re.I)
                    or "action is required before access" in detail_html.lower()
                    or re.search(r"<h1[^>]*>[^<]{5,}", detail_html, re.I)
                ):
                    last_err = "NOT_DETAIL"
                    continue
                return {
                    "ok": True,
                    "status": "DETAIL_OPENED",
                    "via": "interception_open_solicitation",
                    "interception_href": href[:220],
                    "detail_url": final[:220],
                    "solicitation_id": cand.get("solicitation_id"),
                    "result_title": (cand.get("title") or "")[:160] or None,
                    "html": detail_html,
                    "access_gate": (
                        "ACTION_REQUIRED"
                        if "action is required before access" in detail_html.lower()
                        else None
                    ),
                }
            except Exception as exc:
                last_err = type(exc).__name__
                continue
        return {
            "ok": False,
            "status": last_err or "INTERCEPTION_NAV_FAILED",
            "query": q or None,
            "candidates": len(ordered),
        }

    def discover_attachments_by_title(
        self,
        title: str,
        *,
        timeout_ms: int = 20_000,
    ) -> list[dict[str, Any]]:
        """When detail URL variants 404/Welcome, search private BidNet by title/id then scrape docs."""
        if not self.is_authenticated or self._page is None:
            return []
        q = (title or "").strip()
        # Numeric statewide/solicitation ids are short but highly selective
        if len(q) < 6 and not re.fullmatch(r"\d{6,}", q):
            return []
        q = re.sub(r"\s+", " ", q)[:120]

        try:
            resolved = self.resolve_detail_via_authenticated_search(
                title=q, timeout_ms=timeout_ms
            )
            if resolved.get("ok") and resolved.get("detail_url"):
                detail = str(resolved["detail_url"])
                docs = self.discover_attachment_links(detail, timeout_ms=timeout_ms) or []
                if docs:
                    return docs
                # Also try private supplier documents path from solicitation id
                sid = resolved.get("solicitation_id")
                if sid:
                    for cand in (
                        f"https://www.bidnetdirect.com/private/solicitations/{sid}/documents",
                        f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid}/documents",
                        f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid}/view",
                    ):
                        docs = self.discover_attachment_links(cand, timeout_ms=timeout_ms) or []
                        if docs:
                            return docs
        except Exception as exc:
            log.info("discover_attachments_by_title failed: %s", type(exc).__name__)
        return []

    def discover_attachments_from_statewide(
        self,
        statewide_url: str,
        *,
        title: str | None = None,
        statewide_id: str | None = None,
        timeout_ms: int = 20_000,
    ) -> list[dict[str, Any]]:
        """Resolve statewide abstract → private solicitation → attachment hrefs.

        Statewide IDs are a different namespace from private solicitation IDs; never
        invent /private/.../{statewide_id}/view.
        """
        if not self.is_authenticated or self._page is None or not statewide_url:
            return []
        from bidnet_engine.package_materialization import (
            extract_private_solicitation_refs_from_html,
            extract_statewide_id,
        )

        sw = statewide_id or extract_statewide_id(statewide_url)
        try:
            html, payloads = self.navigate_and_collect_json(statewide_url, timeout_ms=timeout_ms)
        except Exception:
            try:
                html = self.fetch_html(statewide_url, timeout_ms=timeout_ms)
                payloads = []
            except Exception:
                html, payloads = "", []

        # Click through common "View solicitation" / "Go to bid" CTAs
        for label in (
            r"View\s*(Solicitation|Bid|Opportunity)",
            r"Go\s*to\s*(Solicitation|Bid)",
            r"Open\s*(Solicitation|Bid)",
            r"Documents",
            r"Attachments",
        ):
            try:
                loc = self._page.get_by_role("link", name=re.compile(label, re.I))
                if loc.count() > 0:
                    loc.first.click(timeout=3_000)
                    self._page.wait_for_timeout(1_200)
                    html = self._page.content() or html
                    break
            except Exception:
                pass

        refs: list[str] = []
        try:
            refs.extend(extract_private_solicitation_refs_from_html(html or "", base_url=statewide_url))
        except Exception:
            pass
        for payload in payloads or []:
            try:
                blob = str(payload)
                refs.extend(extract_private_solicitation_refs_from_html(blob, base_url=statewide_url))
            except Exception:
                continue
        # Dedupe
        seen: set[str] = set()
        uniq_refs: list[str] = []
        for r in refs:
            if r and r not in seen and "/statewide/" not in r.lower():
                seen.add(r)
                uniq_refs.append(r)
        def _score_docs(docs: list[dict[str, Any]]) -> int:
            score = len(docs) * 2
            for d in docs or []:
                fn = str(d.get("filename") or d.get("document_name") or "").lower()
                if d.get("local_path"):
                    score += 6
                if re.search(
                    r"pric|schedule|bid\s*form|item|bom|equip|material|spec|\.xlsx|\.xls|\.csv|\.zip",
                    fn,
                    re.I,
                ):
                    score += 10
                if re.search(r"notice|instruction|cover|terms\s+and|toc\b|agenda", fn, re.I):
                    score -= 3
            return score

        def _merge(dst: list[dict[str, Any]], src: list[dict[str, Any]], seen: set[str]) -> None:
            for d in src or []:
                key = str(d.get("local_path") or d.get("document_url") or d.get("source_url") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                dst.append(d)

        merged: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        best: list[dict[str, Any]] = []
        best_score = -1
        for priv in uniq_refs[:6]:
            docs = self.discover_attachment_links(priv, timeout_ms=timeout_ms) or []
            _merge(merged, docs, seen_keys)
            sc = _score_docs(docs)
            if sc > best_score:
                best, best_score = docs, sc

        # Keyword search: statewide id first (often indexed), then title — always try;
        # first private hit is often cover/notice PDFs, not the schedule package.
        queries: list[str] = []
        if sw:
            queries.append(sw)
        if title and len(str(title).strip()) >= 8:
            queries.append(str(title).strip()[:120])
        for q in queries:
            docs = self.discover_attachments_by_title(q, timeout_ms=timeout_ms) or []
            _merge(merged, docs, seen_keys)
            sc = _score_docs(docs)
            if sc > best_score:
                best, best_score = docs, sc

        # Prefer the richest merged package when it clearly beats a single route
        if _score_docs(merged) >= max(best_score, 0) + 4 and len(merged) > len(best):
            return merged[:40]
        if best:
            return best[:40]
        return merged[:40]

    def navigate_and_collect_json(self, url: str, *, timeout_ms: int = 25_000) -> tuple[str, list[Any]]:
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
        # Hard caps — individual calls may still pass lower timeouts
        try:
            self._page.set_default_timeout(15_000)
            self._page.set_default_navigation_timeout(20_000)
        except Exception:
            pass

    def _probe_authenticated(self) -> dict[str, Any]:
        assert self._page is not None
        url = self.config.verify_url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=3_000)
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
            self._page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=25_000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=3_000)
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
                self._page.wait_for_load_state("domcontentloaded", timeout=25_000)
            except Exception:
                pass
            try:
                self._page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            # Login redirects can still be in-flight after networkidle
            try:
                self._page.wait_for_timeout(800)
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

        html = ""
        current = ""
        for _ in range(4):
            try:
                current = self._page.url or ""
                html = self._page.content()
                break
            except Exception:
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except Exception:
                    pass
                try:
                    self._page.wait_for_timeout(500)
                except Exception:
                    pass
        if not html:
            result = AuthResult(
                status=AUTH_FAILED,
                authenticated=False,
                message="login_content_unavailable_after_submit",
            )
            record_auth_event(AUTH_FAILED, login_failure=True, failure_reason=result.message)
            return result
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
