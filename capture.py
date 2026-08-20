"""
XHS Shots — capture engine
========================================
Drives a real Chromium browser via Playwright. Supports two auth methods:
  1. Persistent browser profile (login once via headed browser)
  2. Manual cookie paste (from browser DevTools — most reliable for headless)

Capture modes
-------------
  full       : full-page screenshot of the note
  viewport   : only the currently visible viewport
  element    : screenshot a single element matched by a CSS selector
  keyword    : scroll the page, find the first element containing the keyword,
               screenshot that element (and its nearest content block)
  region     : screenshot a fixed pixel rectangle x,y,w,h (relative to viewport)
  note       : only the note body content area (auto-detects container)
  to_keyword : full-width screenshot from page TOP down to where the keyword
               appears (横向全屏，纵向截到关键词位置)
"""

from __future__ import annotations

import json
import re
import time
import random
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from playwright.sync_api import sync_playwright, BrowserContext, Page, Error as PWError

XHS_HOSTS = ("xiaohongshu.com", "xhslink.com")
REDDIT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

LOGIN_WALL_SELECTORS = [
    ".login-container",
    "#login-container",
    "div.login",
    ".reds-login",
    ".sign-container",
    "[class*='login-mask']",
    "[class*='captcha-container']",
]
LOGIN_WALL_TEXT = ["登录后查看", "扫码登录", "短信登录", "请先登录", "登录小红书"]

# HARD BLOCK: IP-risk / network-risk error page (error 300012). The note content
# is NEVER served by XHS in this case — there is nothing behind the overlay to
# screenshot. We must detect this and report it clearly rather than trying to
# dismiss it (which would leave an empty page).
HARD_BLOCK_SELECTORS = [
    ".fe-verify-box",
    "[class*='fe-verify']",
    "[class*='verify-box']",
]
HARD_BLOCK_TEXT = ["安全限制", "IP存在风险", "网络环境异常", "300012", "300013"]

# Selectors for the NOTE CONTENT AREA (excludes sidebar, nav, recommendations).
# Priority order: try each until one matches.
NOTE_CONTAINER_SELECTORS = [
    "#noteContainer",           # desktop main note
    ".content-wrapper",        # desktop wrapper
    ".note-content",           # generic note body
    "section.note",            # alternate
    "article",                 # fallback: semantic <article>
    ".detail-container",       # another variant
    "#detail-page",            # full detail page block
]

COOKIE_FILE = Path(__file__).parent / "cookies.json"

# Stronger anti-bot stealth: hides Playwright/automation fingerprints and
# makes the browser report realistic, human-like capabilities.
STEALTH_JS = """
() => {
  // hide navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // spoof plugins (real Chrome has a few)
  Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(i => ({ name: 'Plugin ' + i, description: 'desc ' + i, filename: 'plugin' + i + '.dll' }))
  });
  Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

  // realistic hardware values
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

  // platform
  Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });

  // spoof chrome object presence
  if (!window.chrome) { window.chrome = {}; }
  window.chrome.runtime = {};

  // WebGL vendor / renderer
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter(parameter);
  };

  // override permissions to look normal
  const originalQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters);
}
"""


class LoginWallError(Exception):
    """Raised when the note page is behind a login / captcha wall."""


class CaptureError(Exception):
    """Generic capture failure."""


# ---- cookie helpers ---------------------------------------------------------

def save_cookies(cookies_text: str) -> int:
    """Parse a raw cookie string (from browser DevTools > Application > Cookies)
    and save as JSON. Returns number of cookies parsed.

    Accepts formats:
      - name=value; name2=value2; ...   (simple header-style)
      - JSON array of cookie objects     (exported from DevTools)
    """
    text = cookies_text.strip()
    if not text:
        raise CaptureError("Cookie 内容为空")
    # Try JSON first
    if text.startswith("["):
        try:
            cookies = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"Cookie JSON 格式错误: {exc}")
        _write_cookie_file(cookies)
        return len(cookies)
    # Parse semicolon-separated "name=value; ..." format
    cookies = []
    for part in text.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name and value:
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".xiaohongshu.com",
                "path": "/",
            })
    if not cookies:
        raise CaptureError("未能从文本中解析出任何 Cookie（格式应为 name=value; name2=value2 或 JSON 数组）")
    _write_cookie_file(cookies)
    return len(cookies)


def _write_cookie_file(cookies: list[dict]):
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cookies() -> list[dict]:
    """Load saved cookies. Returns [] if no file."""
    if not COOKIE_FILE.exists():
        return []
    try:
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def clear_cookies() -> None:
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


def cookie_status() -> str:
    """Return a human-readable status string for the UI."""
    c = load_cookies()
    if not c:
        return "未配置"
    names = [ck.get("name", "?") for ck in c[:5]]
    extra = f" (+{len(c)-5}条)" if len(c) > 5 else ""
    return f"已配置 {len(c)} 条 Cookie（{', '.join(names)}{extra}）"


def is_xhs_url(url: str) -> bool:
    return any(h in url for h in XHS_HOSTS)


def _col_to_idx(col) -> int:
    """Accept 'A', 'B', 1, 2, ... -> 1-based index."""
    if isinstance(col, int):
        return col
    s = str(col).strip()
    if s.isdigit():
        return int(s)
    idx = 0
    for ch in s.upper():
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
        else:
            raise ValueError(f"Bad column name: {col}")
    return idx


class CaptureEngine:
    def __init__(
        self,
        profile_dir: str | Path,
        headless: bool = True,
        viewport: Optional[dict] = None,
        mobile: bool = False,
        wait_timeout: int = 20000,
        stealth: bool = True,
        humanize: bool = True,
        cookies: Optional[list[dict]] = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.mobile = mobile
        self.viewport = viewport or (
            {"width": 414, "height": 896} if mobile else {"width": 1280, "height": 900}
        )
        self.wait_timeout = wait_timeout
        self.stealth = stealth
        self.humanize = humanize
        self._cookies = cookies or []
        self._pw = None
        self._ctx: Optional[BrowserContext] = None
        self._ctx_dead = False
        self._warmed_up = False

    # ---- context lifecycle -------------------------------------------------
    def __enter__(self):
        self._pw = sync_playwright().start()
        launch_kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport=self.viewport,
            locale="zh-CN",
            user_agent=REDDIT_UA if self.mobile else DESKTOP_UA,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        # Inject saved cookies (most reliable auth for headless mode)
        if self._cookies:
            try:
                self._ctx.add_cookies(self._cookies)
            except PWError:
                pass  # cookies may be malformed; still try the request
        if self.stealth:
            self._ctx.add_init_script(STEALTH_JS)
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---- login helper -------------------------------------------------------
    def open_login_page(self) -> Page:
        """Open a headed browser on Xiaohongshu for the user to log in manually.
        Returns the page; the caller keeps the engine open until the user is done."""
        if self._ctx is None:
            raise CaptureError("Engine not started. Use `with CaptureEngine(...) as e:`")
        page = self._ctx.new_page()
        page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        return page

    # ---- human-like behavior ------------------------------------------------
    def _human_mouse(self, page: Page):
        """Move the mouse around in a few curved, uneven steps (like a real hand)."""
        vw, vh = self.viewport["width"], self.viewport["height"]
        for _ in range(random.randint(3, 6)):
            x = random.randint(0, vw)
            y = random.randint(0, vh)
            try:
                page.mouse.move(x, y, steps=random.randint(5, 20))
            except PWError:
                pass
            time.sleep(random.uniform(0.1, 0.4))

    def _human_scroll(self, page: Page, chunks: int = 4):
        """Scroll down in uneven chunks with pauses, then glance back up."""
        for _ in range(chunks):
            dy = random.randint(300, 900)
            try:
                page.mouse.wheel(0, dy)
            except PWError:
                pass
            time.sleep(random.uniform(0.5, 1.5))
        # occasional re-read: scroll up a bit
        try:
            page.mouse.wheel(0, -random.randint(100, 400))
        except PWError:
            pass
        time.sleep(random.uniform(0.3, 0.8))

    def _warm_up(self, page: Page):
        """Visit the homepage first to establish a natural, logged-in session
        (cookies already injected) — mimics a real user landing before a note."""
        try:
            page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=10000)
            time.sleep(random.uniform(0.8, 2.0))
            self._human_mouse(page)
            self._human_scroll(page, chunks=1)
        except PWError:
            pass

    # ---- internals ----------------------------------------------------------
    def _wait_for_note(self, page: Page):
        # Give lazy content a moment, then wait for either content or a login wall.
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWError:
            pass
        # Try to wait for a plausible note element (desktop or mobile).
        candidates = [
            "#detail-title",
            ".note-content",
            ".cover",
            "article",
            ".note-card",
            "#noteContainer",
            ".content-wrapper",
        ]
        for sel in candidates:
            try:
                page.wait_for_selector(sel, timeout=1500, state="attached")
                break
            except PWError:
                continue
        # small human-like pause
        time.sleep(random.uniform(0.3, 0.8))

    def _detect_login_wall(self, page: Page) -> bool:
        """Detect a *dismissible* login / anti-crawler modal that sits ON TOP of a
        note that has already loaded. Closing it reveals the note content."""
        url = page.url.lower()
        if "login" in url or "signin" in url or "captcha" in url:
            # 'verify' is intentionally excluded here: the fe-verify-box (error
            # 300012) is a HARD block, not a dismissible modal — handled by
            # _detect_hard_block() instead.
            return True
        for sel in LOGIN_WALL_SELECTORS:
            if page.query_selector(sel):
                return True
        try:
            txt = page.inner_text("body") or ""
        except PWError:
            txt = ""
        for kw in LOGIN_WALL_TEXT:
            if kw in txt:
                return True
        return False

    def _detect_hard_block(self, page: Page) -> str | None:
        """Detect a HARD network/IP block (fe-verify-box, error 300012). Returns the
        error text if blocked, else None. The note content is never served in this
        case, so there is nothing to dismiss or screenshot."""
        url = page.url.lower()
        if "300012" in url or "300013" in url or "website-login/error" in url:
            return "IP/网络风险拦截（小红书拒绝加载内容）"
        for sel in HARD_BLOCK_SELECTORS:
            el = page.query_selector(sel)
            if el:
                try:
                    txt = el.inner_text() or ""
                except PWError:
                    txt = ""
                return txt.strip() or "IP/网络风险拦截"
        try:
            txt = page.inner_text("body") or ""
        except PWError:
            txt = ""
        for kw in HARD_BLOCK_TEXT:
            if kw in txt:
                return f"检测到风控拦截：{kw}"
        return None

    def _detect_rate_limit(self, page: Page) -> bool:
        """Detect a SOFT rate-limit page (the note never loads, but the page does).

        XHS shows 访问过于频繁 / 操作过于频繁 / 频率限制 / 429 / too many requests
        instead of the note when it throttles a single IP. We raise this so the
        batch loop can back off rather than fail every remaining note.
        """
        try:
            txt = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except PWError:
            return False
        return bool(
            re.search(
                r"访问过于频繁|操作过于频繁|频率限制|请求过于频繁|限流|429|"
                r"too many requests|rate.?limit|abuse detection",
                txt,
                re.I,
            )
        )

    def _dismiss_login_wall(self, page: Page):
        """Best-effort: close the anti-crawler / login modal overlay so the note
        content behind it becomes visible — WITHOUT requiring login.

        Covers the common cases:
          * a dismissible login/verify modal (click its close button)
          * an injected overlay element (removed via JS)
          * XHS "login to comment" / engagement bar at the bottom of notes

        Never raises; purely best-effort. If the wall is a hard login *redirect*
        (the whole page is /login) there is nothing to dismiss and the screenshot
        will simply show that page.

        IMPORTANT: we do NOT press ESC here. On XHS desktop web, notes are shown
        as a modal overlay; pressing ESC closes the note modal and reveals the
        explore feed page underneath — which defeats the purpose of capturing the
        note. Close-button clicking + JS removal is sufficient.
        """
        # 1) click any close button on the modal
        close_selectors = [
            ".login-container .close",
            "#login-container .close",
            "div.login .close",
            ".reds-login .close",
            ".sign-container .close",
            "[class*='login'] [class*='close']",
            "button[aria-label='关闭']",
            ".close-button",
            ".modal-close",
            # XHS note-modal close (the × at top-left of note detail)
            ".note-detail-container .close",
            "[class*='note-detail'] [class*='close']",
            "[class*='noteDetail'] [class*='close']",
            "[class*='detail-close']",
        ]
        for sel in close_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=1500)
                    time.sleep(0.5)
                    break
            except PWError:
                continue

        # 2) remove the overlay element(s) via JS as a last resort.
        #    This covers: login modals, anti-crawler popups, captcha sliders,
        #    AND the "登录后评论" / engagement bar that appears at the bottom
        #    of XHS notes when the user is not logged in.
        try:
            page.evaluate(
                """() => {
                    const sels = [
                        // Login / sign-in modals
                        '.login-container', '#login-container', 'div.login',
                        '.reds-login', '.sign-container', '.login-mask',
                        '.modal-mask', '.verify-modal', '[class*="captcha"]',
                        '[class*="login-mask"]', '[class*="slider-captcha"]',
                        // XHS-specific: comment-area login prompt ("登录后评论")
                        '[class*="comment-login"]',
                        '[class*="comment"][class*="login"]',
                        '[class*="comment"][class*="signin"]',
                        // Generic overlay patterns
                        '[class*="auth-overlay"]',
                        '[class*="sign-overlay"]',
                    ];
                    sels.forEach(s => {
                        document.querySelectorAll(s).forEach(n => n.remove());
                    });
                    document.body.style.overflow = '';
                }"""
            )
            time.sleep(0.5)
        except PWError:
            pass

    def _hide_sticky_headers(self, page: Page):
        """Hide sticky / fixed header bars that sit above the note content.

        On XHS desktop, a global navigation bar (or debug overlay) can appear at the
        very top of #noteContainer.  It covers the author name when the screenshot is
        cropped to [author_top, first_image_bottom].  We hide it via JS so the
        element.screenshot() captures only the clean note card.
        """
        try:
            page.evaluate(
                """() => {
                    // --- Phase 1: known sticky-header selectors ---
                    const sels = [
                        // XHS desktop global nav / top bar
                        '.global-nav', '#global-nav',
                        '[class*="top-bar"]', '[class*="topbar"]',
                        '[class*="sticky-header"]', '[class*="sticky-nav"]',
                        '.header-bar', '#header-bar',
                        // Generic fixed/sticky positioned headers
                        'header[style*="fixed"]', 'header[style*="sticky"]',
                        'nav[style*="fixed"]',   'nav[style*="sticky"]',
                        '[class*="navbar"]', '[class*="toolbar"]',
                        '[id*="navbar"]',   '[id*="toolbar"]',
                        // XHS-specific thin bars above note detail
                        '.note-detail-bar', '.detail-top-bar',
                        '[class*="detail-head"]',
                    ];
                    let hidden = 0;
                    sels.forEach(s => {
                        document.querySelectorAll(s).forEach(el => {
                            el.style.setProperty('display', 'none', 'important');
                            hidden++;
                        });
                    });

                    // --- Phase 2: brute-force any position:fixed/absolute element
                    // whose bounding box overlaps the TOP of #noteContainer.
                    const nc = document.querySelector('#noteContainer')
                           || document.querySelector('.note-container')
                           || document.querySelector('.content-wrapper');
                    if (nc) {
                        const ncRect = nc.getBoundingClientRect();
                        const ncTop = ncRect.top;
                        document.querySelectorAll('*').forEach(el => {
                            try {
                                const cs = getComputedStyle(el);
                                const pos = cs.position;
                                if (pos !== 'fixed' && pos !== 'absolute') return;
                                const r = el.getBoundingClientRect();
                                // Element is near or above the note container's top edge
                                // AND it's not deeply nested inside the note body itself.
                                if (r.bottom <= ncTop + 8 && r.width > 50) {
                                    el.style.setProperty('display', 'none', 'important');
                                    hidden++;
                                }
                            } catch(e) { /* skip */ }
                        });
                    }

                    return hidden;
                }"""
            )
        except PWError:
            pass

    @staticmethod
    def _trim_dark_borders(img_path, threshold=40):
        """Trim solid dark borders from a screenshot image.

        XHS pages sometimes render a thin dark frame around #noteContainer
        (page background bleeding through padding/margin).  This post-process
        step crops away uniform dark borders so only the clean content remains.

        Args:
            img_path: path to the PNG file (modified in-place).
            threshold: max brightness (0-255) for a pixel to be considered "dark".
        """
        from PIL import Image as _PILImage
        import os as _os

        try:
            with _PILImage.open(img_path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                w, h = im.size
                if w < 10 or h < 10:
                    return

                def _is_dark_row(y):
                    """Check if row y is predominantly dark."""
                    total = 0
                    count = 0
                    for x in range(0, w, max(1, w // 100)):
                        r, g, b = im.getpixel((x, y))[:3]
                        total += (r + g + b) / 3
                        count += 1
                    return (total / count) < threshold if count > 0 else False

                def _is_dark_col(x):
                    """Check if column x is predominantly dark."""
                    total = 0
                    count = 0
                    for y in range(0, h, max(1, h // 100)):
                        r, g, b = im.getpixel((x, y))[:3]
                        total += (r + g + b) / 3
                        count += 1
                    return (total / count) < threshold if count > 0 else False

                top = 0
                while top < h and _is_dark_row(top):
                    top += 1
                bottom = h - 1
                while bottom > top and _is_dark_row(bottom):
                    bottom -= 1
                left = 0
                while left < w and _is_dark_col(left):
                    left += 1
                right = w - 1
                while right > left and _is_dark_col(right):
                    right -= 1

                # Only crop if we actually found borders to trim
                if top > 0 or bottom < h - 1 or left > 0 or right < w - 1:
                    cropped = im.crop((left, top, right + 1, bottom + 1))
                    tmp = img_path + ".tmp_trim.png"
                    cropped.save(tmp)
                    _os.replace(tmp, img_path)
        except Exception:
            pass  # non-critical: better to keep original than crash

    def _find_note_container(self, page: Page):
        """Find the main note content element (excludes sidebar, nav, recommendations).

        Important: the returned element must NOT be a full-page wrapper — otherwise the
        screenshot becomes a "whole page" shot (sidebar + nav + recommendations) instead
        of just the note. We therefore (a) skip selectors that are known page-level
        containers, and (b) reject any candidate whose bounding box covers ~the full
        viewport width AND ~the full page height.
        """
        try:
            dims = page.evaluate(
                """() => ({
                    vw: window.innerWidth,
                    vh: window.innerHeight,
                    sh: Math.max(
                        document.documentElement.scrollHeight,
                        document.body.scrollHeight
                    )
                })"""
            )
        except PWError:
            dims = {"vw": self.viewport["width"], "vh": 800, "sh": 800}
        vw = dims.get("vw") or self.viewport["width"]
        sh = dims.get("sh") or vw * 2

        def looks_like_full_page(box) -> bool:
            if not box:
                return False
            # Covers ~full viewport width AND (nearly) the whole scrollable height
            # → that's the page wrapper, not the note body.
            return box["width"] >= vw * 0.9 and box["height"] >= max(sh, 800) * 0.85

        def looks_like_overlay(el) -> bool:
            """Reject elements that are clearly popups / verify boxes / modals,
            not the actual note content."""
            try:
                cls = (el.get_attribute("class") or "").lower()
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                id_ = (el.get_attribute("id") or "").lower()
                overlay_keywords = [
                    "verify", "captcha", "login", "sign", "mask", "modal",
                    "overlay", "popup", "dialog", "fe-", "anti-",
                ]
                combined = f"{cls} {id_} {tag}"
                return any(kw in combined for kw in overlay_keywords)
            except PWError:
                return False

        # 1) Try the precise selectors first (skip the explicit full-page block).
        for sel in NOTE_CONTAINER_SELECTORS:
            if sel in ("#detail-page",):
                continue  # that selector is the WHOLE page block — never use it here
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                box = el.bounding_box()
                if box and box["width"] > 200 and box["height"] > 100 and not looks_like_full_page(box) and not looks_like_overlay(el):
                    return el
            except PWError:
                continue

        # 2) Fallback: anchor on the note title, walk up its ancestors and pick the
        #    deepest one that is reasonably sized but NOT a full-page wrapper.
        try:
            handle = page.evaluate_handle(
                """() => {
                    const title = document.querySelector(
                        '#detail-title, h1.title, .title, .note-title, [class*="title"]'
                    );
                    if (!title) return null;
                    const vw = window.innerWidth;
                    const sh = Math.max(
                        document.documentElement.scrollHeight,
                        document.body.scrollHeight
                    );
                    const isFullPage = r => r.width >= vw * 0.9 &&
                        r.height >= Math.max(sh, 800) * 0.85;
                    let el = title.parentElement;
                    let best = null;
                    for (let i = 0; i < 8 && el; i++) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 300 && r.height > 200 && !isFullPage(r)) best = el;
                        el = el.parentElement;
                    }
                    return best;
                }"""
            )
            el = handle.as_element()
            if el:
                box = el.bounding_box()
                if box and not looks_like_full_page(box) and not looks_like_overlay(el):
                    return el
        except PWError:
            pass

        raise CaptureError(
            "未找到笔记内容区域。页面可能：①未加载完 ②触发了登录墙 ③结构异常。\n"
            "建议：改用「element」模式手动指定 CSS 选择器，或改用「full」整页截图。"
        )

    def _find_keyword_element(self, page: Page, keyword: str):
        # Scroll gradually to load lazy content, then locate the element.
        for _ in range(6):
            try:
                page.mouse.wheel(0, 1200)
            except PWError:
                pass
            time.sleep(0.4)
        handle = page.evaluate_handle(
            """(kw) => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    if (node.nodeValue && node.nodeValue.includes(kw)) {
                        let el = node.parentElement;
                        // climb past inline elements
                        while (el && getComputedStyle(el).display === 'inline') {
                            el = el.parentElement;
                        }
                        // expand up to a container with a reasonable box
                        let cur = el;
                        while (cur && cur.parentElement) {
                            const r = cur.getBoundingClientRect();
                            if (r.width > 80 && r.height > 40) break;
                            cur = cur.parentElement;
                        }
                        return cur || el;
                    }
                }
                return null;
            }""",
            keyword,
        )
        el = handle.as_element()
        if not el:
            raise CaptureError(f"Keyword not found on page: {keyword!r}")
        try:
            el.scroll_into_view_if_needed()
        except PWError:
            pass
        time.sleep(0.3)
        return el

    def _note_author_first_image_clip(self, page: Page, note_el):
        """Compute a crop band (relative to the full note-element screenshot) that
        spans from the author block (top) down to the first content image (bottom).

        This satisfies the user's request:
          * "保证作者显示完全"  — the author block is never clipped (we start at its top)
          * "截滚动到第一张图片的" — the capture includes the note's FIRST photo

        The "first content image" must be a REAL note photo, NOT:
          * the author avatar / comment avatars
          * inline emoji / sticker / icon images in the text
          * thumbnails inside the comments or the recommendation sidebar
        We enforce this with (a) a minimum width relative to the card, (b) an
        exclusion list of zones (comment / recommend / avatar / sticker / emoji …),
        and (c) a src-keyword blacklist. Among the survivors we pick the topmost one.

        Returns (y0, y1) in pixels relative to the note element's top-left, or None
        if we can't locate a first image (caller falls back to the full element).
        """
        # Pass the real captured element (not a fresh #noteContainer query) so the
        # crop coordinates line up exactly with note_el.screenshot().
        data = page.evaluate(
            """(nc) => {
                if (!nc) return null;
                const sTop = window.scrollY;
                const ncRect = nc.getBoundingClientRect();
                const ncTop = ncRect.top + sTop;
                const ncW = ncRect.width;

                // ---- author block: topmost non-comment author element ----
                let authorTop = null;
                const aSels = ['.author-wrapper', '.note-info', '.user-info', '#user-info',
                               '.author-info', '.note-header', '.author', '.info .header',
                               '.author-info', '.note-author'];
                aSels.forEach(s => {
                    const el = nc.querySelector(s);
                    if (el) {
                        const dt = el.getBoundingClientRect().top + sTop;
                        if (authorTop === null || dt < authorTop) authorTop = dt;
                    }
                });

                // ---- first CONTENT image (a real note photo) ----
                // Zones whose <img> children must never count as the note photo.
                const EXCLUDE_ZONE = ['comment', 'recommend', 'related', 'similar',
                                      'avatar', 'footer', 'sidebar', 'aside',
                                      'stick', 'emoji', 'mascot', 'expression'];
                const inExcludedZone = (im) => {
                    let el = im;
                    while (el) {
                        const c = (el.className && el.className.toString().toLowerCase()) || '';
                        const id = (el.id || '').toLowerCase();
                        if (EXCLUDE_ZONE.some(k => c.includes(k) || id.includes(k))) return true;
                        el = el.parentElement;
                    }
                    return false;
                };
                const SRC_BLACKLIST = /avatar|no-comments|emoji|sticker|icon|logo|badge|placeholder|face|expression/;
                const minW = Math.max(160, ncW * 0.22);

                const consider = (im) => {
                    if (inExcludedZone(im)) return;
                    const r = im.getBoundingClientRect();
                    if (r.width < minW || r.height < 80) return;
                    const src = (im.getAttribute('src') || '').toLowerCase();
                    if (SRC_BLACKLIST.test(src)) return;
                    const dt = r.top + sTop;
                    const db = r.bottom + sTop;
                    if (!window.__firstImg || dt < window.__firstImg.top) {
                        window.__firstImg = {top: dt, bottom: db};
                    }
                };
                window.__firstImg = null;

                // 1) Prefer a media carousel / image wrapper if the layout has one.
                const mediaEls = nc.querySelectorAll(
                    '.swiper-wrapper, .note-slider, .carousel, .media-wrapper, ' +
                    '.left, .note-image-wrapper, .note-pic, [class*="slider"]'
                );
                mediaEls.forEach(m => m.querySelectorAll('img').forEach(consider));

                // 2) Fallback: scan every <img> in the card.
                if (!window.__firstImg) {
                    nc.querySelectorAll('img').forEach(consider);
                }

                const firstImg = window.__firstImg;
                if (!firstImg) return null;

                const topRef = (authorTop !== null) ? Math.min(authorTop, firstImg.top) : firstImg.top;
                const y0 = Math.max(0, topRef - ncTop);
                // End EXACTLY at the first photo's bottom (+2px anti-clip safety). A larger
                // padding would spill into the next stacked image / caption, violating
                // "截到第一张图片的".
                const y1 = firstImg.bottom - ncTop + 2;
                if (y1 <= y0) return null;
                return {y0: Math.round(y0), y1: Math.round(y1), w: Math.round(ncW)};
            }""",
            note_el,
        )
        if not data:
            return None
        return (data["y0"], data["y1"])

    def _find_keyword_y(self, page: Page, keyword: str, padding: int = 80) -> tuple[int, int]:
        """Scroll through the page to find the keyword, return (clip_height, viewport_height).

        The clip_height = keyword_bottom_y + padding, i.e. how tall the screenshot
        should be from the top of the page down to (and slightly past) the keyword.
        """
        vw = self.viewport["width"]
        vh = self.viewport["height"]

        # First scroll to top, then progressively scroll down searching for keyword
        page.evaluate("() => window.scrollTo(0, 0)")
        time.sleep(0.5)

        max_scrolls = 20  # safety limit
        for attempt in range(max_scrolls):
            # Check if keyword is visible on current screen; also locate interaction bar
            result = page.evaluate(
                """(kw) => {
                    // 1) find keyword Y (first occurrence from top)
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT,
                        { acceptNode: (n) => {
                            const r = n.parentElement.getBoundingClientRect();
                            return (r.top < window.innerHeight && r.bottom > 0 && r.width > 0)
                                ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                        }}
                    );
                    let node;
                    let bestY = -1;
                    while ((node = walker.nextNode())) {
                        if (node.nodeValue && node.nodeValue.includes(kw)) {
                            const rect = node.parentElement.getBoundingClientRect();
                            const y = rect.bottom + window.scrollY;
                            if (bestY < 0 || y < bestY) bestY = y;
                        }
                    }

                    // 2) find interaction bar bottom Y
                    // XHS desktop: the ❤️点赞 ⭐收藏 💬评论 🔁分享 bar at note bottom
                    let barBottom = -1;
                    const BAR_SELECTORS = [
                        '.note-bottom', '.content-interaction', '.interaction-bar',
                        '.note-footer', '[class*="interact"]', '[class*="engagement"]',
                        '#noteContainer [class*="bottom"]', 'section.note [class*="footer"]',
                        '.note-bottom-container'
                    ];
                    for (const sel of BAR_SELECTORS) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                barBottom = r.bottom + window.scrollY;
                                break;
                            }
                        }
                    }
                    // fallback: scan buttons in note container for interaction row
                    if (barBottom < 0) {
                        const noteCont = document.querySelector('#noteContainer')
                                      || document.querySelector('section.note')
                                      || document.querySelector('[class*="note-detail"]');
                        if (noteCont) {
                            const btns = noteCont.querySelectorAll('button, [role="button"], [class*="btn"]');
                            let lastBtnRowBottom = 0;
                            for (const btn of btns) {
                                const r = btn.getBoundingClientRect();
                                if (r.width > 20 && r.height > 20) {
                                    const b = r.bottom + window.scrollY;
                                    // group buttons on same row (within 10px vertical)
                                    if (Math.abs(b - lastBtnRowBottom) < 10 || !lastBtnRowBottom) {
                                        lastBtnRowBottom = Math.max(lastBtnRowBottom || 0, b);
                                    }
                                }
                            }
                            if (lastBtnRowBottom > 0) barBottom = lastBtnRowBottom;
                        }
                    }

                    return {
                        found: bestY >= 0,
                        kwY: bestY,
                        barBottom: barBottom,
                        pageHeight: document.documentElement.scrollHeight
                    };
                }""",
                keyword,
            )
            found = result.get("found", False)
            kw_y = result.get("kwY", 0)
            bar_bottom = result.get("barBottom", -1)
            page_height = result.get("pageHeight", vh)

            if found:
                clip_h = int(kw_y) + padding
                # Extend to include interaction bar if found
                if bar_bottom > 0:
                    clip_h = max(clip_h, int(bar_bottom) + 40)
                return clip_h, vh

            # Keyword not visible yet — scroll down and try again
            page.mouse.wheel(0, vh * 0.7)
            time.sleep(0.5)

        raise CaptureError(
            f"在页面中未找到关键词「{keyword}」。"
            f"已滚动搜索约 {max_scrolls} 屏，请确认关键词是否存在于笔记正文中。"
        )

    def _extract_interactions(self, page: Page) -> dict:
        """Extract engagement metrics (likes / collects / comments / shares)
        from the note page. Returns a dict of human-readable strings."""
        try:
            data = page.evaluate("""() => {
                const result = { likes: '', collects: '', comments: '', shares: '' };
                const KW = {
                    likes:    ['点赞', 'like'],
                    collects: ['收藏', 'collect'],
                    comments: ['评论', 'comment', 'chat'],
                    shares:   ['分享', 'share'],
                };
                // number pattern used by XHS: 1.2万 / 3521 / 999+ / 3.4w
                const NUM_RE = /[\\d.]+\\s*[万wW]?\\+?/;
                function cleanNum(s) {
                    if (!s) return '';
                    const m = s.match(NUM_RE);
                    return m ? m[0].trim() : '';
                }
                // 1) try known wrapper class names (desktop + mobile)
                const SELECTORS = {
                    likes:    ['.like-wrapper', '.likeBtn', '[class*="like"]'],
                    collects: ['.collect-wrapper', '.collectBtn', '[class*="collect"]'],
                    comments: ['.chat-wrapper', '.comment-wrapper', '[class*="chat"]', '[class*="comment"]'],
                    shares:   ['.share-wrapper', '.shareBtn', '[class*="share"]'],
                };
                for (const key in SELECTORS) {
                    for (const sel of SELECTORS[key]) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const num = cleanNum(el.innerText || '');
                            if (num) { result[key] = num; break; }
                        }
                    }
                }
                // 2) fallback: walk elements, match by visible text keyword
                if (!result.likes || !result.collects || !result.comments || !result.shares) {
                    const all = [...document.querySelectorAll('button, span, div, a')];
                    for (const el of all) {
                        const t = (el.innerText || '').trim();
                        if (!t) continue;
                        for (const key in KW) {
                            if (result[key]) continue;
                            if (KW[key].some(k => t.includes(k))) {
                                const num = cleanNum(t);
                                if (num) result[key] = num;
                            }
                        }
                    }
                }
                return result;
            }""")
        except PWError:
            data = {}
        # Normalise: ensure all keys present
        return {
            "likes": (data or {}).get("likes", "") or "",
            "collects": (data or {}).get("collects", "") or "",
            "comments": (data or {}).get("comments", "") or "",
            "shares": (data or {}).get("shares", "") or "",
        }

    def _overlay_stats(self, out_path: str | Path, stats: dict):
        """Composite a banner with engagement stats onto the top of the screenshot.
        Mutates the PNG in place."""
        try:
            with Image.open(out_path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                W, H = im.size
                draw = ImageDraw.Draw(im)
                # pick a font (fallback to default)
                font = None
                for cand in [
                    "/System/Library/Fonts/PingFang.ttc",
                    "/System/Library/Fonts/STHeiti Light.ttc",
                    "/System/Library/Fonts/Hiragino Sans GB.ttc",
                ]:
                    if Path(cand).exists():
                        try:
                            font = ImageFont.truetype(cand, 28)
                            break
                        except OSError:
                            continue
                if font is None:
                    font = ImageFont.load_default()

                parts = []
                if stats.get("likes"):
                    parts.append(f"👍 {stats['likes']}")
                if stats.get("collects"):
                    parts.append(f"⭐ {stats['collects']}")
                if stats.get("comments"):
                    parts.append(f"💬 {stats['comments']}")
                if stats.get("shares"):
                    parts.append(f"🔁 {stats['shares']}")
                if not parts:
                    return  # nothing to draw
                banner_text = "   ".join(parts)

                # measure banner height
                pad = 14
                bbox = draw.textbbox((0, 0), banner_text, font=font)
                text_h = bbox[3] - bbox[1]
                bar_h = text_h + pad * 2

                # draw semi-transparent dark bar at top
                bar = Image.new("RGB", (W, bar_h), (0, 0, 0))
                im.paste(bar, (0, 0))
                draw = ImageDraw.Draw(im)
                # darken overlay ~55%
                draw.rectangle([0, 0, W, bar_h], fill=(20, 20, 20))
                draw.text((pad, pad), banner_text, fill=(255, 255, 255), font=font)
                im.save(out_path)
        except (PWError, OSError):
            pass  # overlay is best-effort; never fail the capture

    # ---- public API ---------------------------------------------------------
    def capture(
        self,
        url: str,
        mode: str = "full",
        *,
        selector: Optional[str] = None,
        keyword: Optional[str] = None,
        region: Optional[tuple] = None,
        out_path: Optional[str | Path] = None,
        show_stats: bool = True,
    ) -> Path:
        """Capture a single URL. Returns the path of the saved PNG.

        Each internal step has its own timeout (goto=12s, selectors=1.5s each).
        Worst case ~25 s per note — no thread tricks, pure sync Playwright.
        """
        if self._ctx is None:
            raise CaptureError("Engine not started. Use `with CaptureEngine(...) as e:`")

        # --- context health check: if it died, try to revive ---
        if not self._is_context_alive():
            try:
                self._ctx.close()
            except PWError:
                pass
            self._recreate_context()

        page = None
        try:
            page = self._ctx.new_page()

            # 1) warm up on homepage ONLY on first capture (flag-based)
            if self.humanize and is_xhs_url(url) and not getattr(self, "_warmed_up", False):
                self._warm_up(page)
                page.close()
                page = self._ctx.new_page()  # fresh page for the actual note
                self._warmed_up = True

            # 2) navigate to the target note
            page.goto(url, wait_until="domcontentloaded", timeout=12000)
            self._wait_for_note(page)

            # 2.5) HARD BLOCK check FIRST (fe-verify-box / error 300012). The note
            # content is never loaded in this case — there is nothing to dismiss or
            # screenshot. Report it clearly instead of producing a garbage image.
            hard_block = self._detect_hard_block(page)
            if hard_block:
                raise CaptureError(
                    f"截图失败：小红书风控拦截（{hard_block}）。\n"
                    "这是 IP/网络级别的限制，无法通过关闭弹窗解决。\n"
                    "建议：①等待 IP 冷却后重试 ②使用干净的代理 IP ③降低请求频率。"
                )

            # 2.6) Dismissible login/anti-crawler modal (note already loaded behind it).
            # Close it so the screenshot shows the note, not the popup.
            if self._detect_login_wall(page):
                self._dismiss_login_wall(page)

            # 2.7) Soft rate-limit detection. When XHS throttles (访问过于频繁 /
            # 429 / 限流), the page still "loads" but the note never appears. Catching
            # this early lets the batch loop back off instead of burning the rest of
            # the run on guaranteed failures (and getting the IP banned harder).
            if self._detect_rate_limit(page):
                raise CaptureError(
                    "截图失败：小红书限流（访问过于频繁 / 429）。\n"
                    "建议：降低请求频率，拉长间隔，稍后重试。"
                )

            # 3) simulate reading: mouse moves + scroll
            if self.humanize:
                self._human_mouse(page)
                self._human_scroll(page, chunks=random.randint(2, 4))
                time.sleep(random.uniform(0.5, 1.2))

            if mode == "full":
                page.screenshot(path=str(out_path), full_page=True)
            elif mode == "note":
                # Only screenshot the note content area (no sidebar, no recommendations).
                # Crop to author block (top) → first content image (bottom) so the
                # author is always fully shown and the first image is included.
                note_el = self._find_note_container(page)
                # Hide sticky / fixed header bars that sit above the note content
                # (e.g. XHS global nav bar).  Without this, a dark strip at the top
                # of #noteContainer covers part of the author name.
                self._hide_sticky_headers(page)
                clip = self._note_author_first_image_clip(page, note_el)
                if clip:
                    import tempfile as _tf
                    import os as _os
                    from PIL import Image as _Image

                    tmp = str(out_path) + ".full.png"
                    note_el.screenshot(path=tmp)
                    with _Image.open(tmp) as _im:
                        y0, y1 = clip
                        y1 = min(y1, _im.height)
                        _im.crop((0, y0, _im.width, y1)).save(str(out_path))
                    try:
                        _os.remove(tmp)
                    except OSError:
                        pass
                else:
                    note_el.screenshot(path=str(out_path))
                # Post-process: trim any remaining dark borders (page background
                # bleeding through #noteContainer padding/margin).
                self._trim_dark_borders(str(out_path))
            elif mode == "viewport":
                page.screenshot(path=str(out_path))
            elif mode == "element":
                if not selector:
                    raise CaptureError("element mode requires `selector`")
                el = page.query_selector(selector)
                if not el:
                    raise CaptureError(f"Element not found for selector: {selector}")
                el.screenshot(path=str(out_path))
            elif mode == "keyword":
                if not keyword:
                    raise CaptureError("keyword mode requires `keyword`")
                el = self._find_keyword_element(page, keyword)
                el.screenshot(path=str(out_path))
            elif mode == "to_keyword":
                if keyword:
                    # Full-width screenshot from page TOP down to where the keyword appears
                    clip_h, vh = self._find_keyword_y(page, keyword)
                    # Scroll back to top for the screenshot
                    page.evaluate("() => window.scrollTo(0, 0)")
                    time.sleep(0.3)
                    page.screenshot(
                        path=str(out_path),
                        clip={"x": 0, "y": 0, "width": self.viewport["width"], "height": clip_h},
                        full_page=False,
                    )
                else:
                    # No keyword supplied → just capture the whole note content area.
                    # (User intent: "don't need to find a keyword", so fall back to
                    # the full note body, no sidebar / recommendations.)
                    note_el = self._find_note_container(page)
                    note_el.screenshot(path=str(out_path))
                    self._trim_dark_borders(str(out_path))
            elif mode == "region":
                if not region or len(region) != 4:
                    raise CaptureError("region mode requires `region=(x,y,w,h)`")
                x, y, w, h = region
                page.screenshot(path=str(out_path), clip={"x": x, "y": y, "width": w, "height": h})
            else:
                raise CaptureError(f"Unknown mode: {mode}")

            # Optional: overlay engagement stats (likes/collects/comments/shares)
            if show_stats:
                try:
                    stats = self._extract_interactions(page)
                    if any(stats.values()):
                        self._overlay_stats(out_path, stats)
                except Exception:  # noqa: BLE001
                    pass  # best-effort, never fail the capture
            return Path(out_path)
        except PWError as exc:
            err_msg = str(exc).lower()
            if "context" in err_msg or "closed" in err_msg or "target closed" in err_msg:
                # Context died mid-capture; mark it dead so next call recreates it.
                self._ctx_dead = True
                raise CaptureError(f"浏览器上下文异常（{exc}），将自动恢复后继续下一条。")
            raise CaptureError(f"浏览器错误：{exc}")
        finally:
            try:
                if page:
                    page.close()
            except PWError:
                pass

    # ---- context recovery ----------------------------------------------------
    def _is_context_alive(self) -> bool:
        """Quick check whether the current context is still usable."""
        if getattr(self, "_ctx_dead", False):
            return False
        if self._ctx is None:
            return False
        try:
            # Try a cheap operation — if this throws, context is dead.
            self._ctx.pages  # noqa: B018
            return True
        except PWError:
            return False

    def _recreate_context(self):
        """Rebuild the browser context from scratch (keeps same profile dir + cookies)."""
        self._pw = self._pw or sync_playwright().start()
        launch_kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport=self.viewport,
            locale="zh-CN",
            user_agent=REDDIT_UA if self.mobile else DESKTOP_UA,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        if self.stealth:
            self._ctx.add_init_script(STEALTH_JS)
        if self._cookies:
            try:
                self._ctx.add_cookies(self._cookies)
            except PWError:
                pass
        self._ctx_dead = False
        self._warmed_up = False  # re-warm on new context


def sanitize_filename(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "_", s).strip("_")
    return s[:max_len] or "note"
