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
]
LOGIN_WALL_TEXT = ["登录后查看", "扫码登录", "短信登录", "请先登录", "登录小红书"]

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
        url = page.url.lower()
        if "login" in url or "signin" in url or "captcha" in url or "verify" in url:
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

    def _dismiss_login_wall(self, page: Page):
        """Best-effort: close the anti-crawler / login modal overlay so the note
        content behind it becomes visible — WITHOUT requiring login.

        Covers the common cases:
          * a dismissible login/verify modal (press ESC, click its close button)
          * an injected overlay element (removed via JS)
        Never raises; purely best-effort. If the wall is a hard login *redirect*
        (the whole page is /login) there is nothing to dismiss and the screenshot
        will simply show that page.
        """
        # 1) ESC often closes a modal
        try:
            page.keyboard.press("Escape")
        except PWError:
            pass
        # 2) click any close button on the modal
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
        ]
        for sel in close_selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
                    break
            except PWError:
                continue
        # 3) remove the overlay element(s) via JS as a last resort
        try:
            page.evaluate(
                """() => {
                    const sels = [
                        '.login-container', '#login-container', 'div.login',
                        '.reds-login', '.sign-container', '.login-mask',
                        '.modal-mask', '.verify-modal', '[class*="captcha"]',
                        '[class*="login-mask"]', '[class*="slider-captcha"]'
                    ];
                    sels.forEach(s => {
                        document.querySelectorAll(s).forEach(n => n.remove());
                    });
                    document.body.style.overflow = '';
                }"""
            )
        except PWError:
            pass
        time.sleep(0.5)

    def _find_note_container(self, page: Page):
        """Find the main note content element (excludes sidebar, nav, recommendations).
        Returns the element, or raises CaptureError if not found."""
        for sel in NOTE_CONTAINER_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el:
                    # Make sure it has some visible size
                    box = el.bounding_box()
                    if box and box["width"] > 200 and box["height"] > 100:
                        return el
            except PWError:
                continue
        # Last resort: try to find by excluding known sidebar/nav elements
        try:
            handle = page.evaluate_handle("""() => {
                // Look for the main content area that contains the note title
                const title = document.querySelector('#detail-title, h1.title, .title');
                if (title) {
                    // Walk up to find a container that's reasonably sized
                    let el = title.parentElement;
                    for (let i = 0; i < 6 && el; i++) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 300 && r.height > 200) return el;
                        el = el.parentElement;
                    }
                }
                return null;
            }""")
            el = handle.as_element()
            if el:
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
            if self._detect_login_wall(page):
                # Anti-crawler wall detected: close the overlay and screenshot the
                # content behind it WITHOUT requiring login (per user request).
                self._dismiss_login_wall(page)

            # 3) simulate reading: mouse moves + scroll
            if self.humanize:
                self._human_mouse(page)
                self._human_scroll(page, chunks=random.randint(2, 4))
                time.sleep(random.uniform(0.5, 1.2))

            if mode == "full":
                page.screenshot(path=str(out_path), full_page=True)
            elif mode == "note":
                # Only screenshot the note content area (no sidebar, no recommendations)
                note_el = self._find_note_container(page)
                note_el.screenshot(path=str(out_path))
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
