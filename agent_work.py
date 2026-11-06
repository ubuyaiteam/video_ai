import argparse
import base64
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from cookies_util import load_cookies

AGENT_URL = "https://agent.digen.ai/en"
MODE_TEXT = "Movie"            # fallback chip on the home screen
SKILL_NAME = "Movie Maker"     # entry in the "Select Skill" dialog
PROMPT_SELECTOR = 'div[role="textbox"]:visible'
IDLE_DONE_S = 90         # agent not busy for this long + a video found -> finished
POST_DONE_WAIT_S = 90    # after "This round consumed", keep looking this long for a video

# Text that is on the page while the agent is still working.
BUSY_RE = re.compile(r"Digen is thinking|Generating (?:video|image)|Thinking \d+:\d{2}", re.I)

# Credit balance shown in the header: <span title="Meme"> ... <span class="tabular-nums">249</span>
# Tries the exact selector first, then looser ones, then the rightmost plain number in the top bar.
CREDITS_JS = """
() => {
  const el = document.querySelector('span[title="Meme"] span.tabular-nums')
          || document.querySelector('button span.tabular-nums')
          || document.querySelector('span.tabular-nums');
  if (el) {
    const n = parseInt(el.textContent.replace(/[^0-9]/g, ''), 10);
    return Number.isNaN(n) ? null : n;
  }
  let best = null;
  document.querySelectorAll('body *').forEach(e => {
    if (e.children.length) return;
    const t = e.textContent.trim();
    if (!/^[\\d,]{1,9}$/.test(t)) return;
    const r = e.getBoundingClientRect();
    if (!(r.width > 0 && r.top >= 0 && r.top < 90)) return;
    if (!best || r.left > best.r.left) best = {r, t};
  });
  return best ? parseInt(best.t.replace(/,/g, ''), 10) : null;
}
"""

# Debug helper: lists the text elements in the top bar (saved when credits can't be read).
TOP_DUMP_JS = """
() => Array.from(document.querySelectorAll('body *')).filter(e => {
  const r = e.getBoundingClientRect();
  return r.top >= 0 && r.top < 90 && r.width > 0 && e.children.length === 0 && e.textContent.trim();
}).map(e => e.tagName + '.' + e.className + ' => ' + e.textContent.trim().slice(0, 40)).join('\\n')
"""

# Collects every video-ish URL in a frame: <video>, <source>, mp4/webm links and
# data-* attributes, including inside shadow roots.
ALL_SRC_JS = r"""
() => {
  const out = [];
  const isVid = u => /\.(mp4|webm|mov)(\?|#|$)/i.test(u || '');
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === 'VIDEO') {
        if (el.currentSrc) out.push(el.currentSrc);
        if (el.getAttribute('src')) out.push(abs(el.getAttribute('src')));
      }
      for (const a of ['src', 'href', 'data-src', 'data-url', 'data-video', 'data-video-url']) {
        const v = el.getAttribute(a);
        if (v && (el.tagName === 'SOURCE' || isVid(v))) out.push(abs(v));
      }
    });
  };
  walk(document);
  return out.filter(Boolean);
}
"""

# Video URLs inside JSON / text API responses.
VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:mp4|webm|mov)(?:\?[^\s\"'<>\\]*)?", re.I)

BLOB_JS = """
async (u) => {
  const b = await (await fetch(u)).blob();
  return await new Promise(res => {
    const fr = new FileReader();
    fr.onloadend = () => res(fr.result.split(',')[1]);
    fr.readAsDataURL(b);
  });
}
"""

# Send button = rightmost button on the same row as the "Select skill" button.
SEND_POS_JS = """
() => {
  const ref = document.querySelector('button[aria-label="Select skill"]');
  if (!ref) return null;
  const rr = ref.getBoundingClientRect();
  const cy = rr.top + rr.height / 2;
  let best = null;
  document.querySelectorAll('button').forEach(b => {
    const r = b.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return;
    if (Math.abs((r.top + r.height / 2) - cy) > 14) return;
    if (r.left <= rr.left) return;
    if (!best || r.left > best.r.left) best = {r, b};
  });
  if (!best) return null;
  return {
    x: best.r.left + best.r.width / 2,
    y: best.r.top + best.r.height / 2,
    disabled: best.b.className.includes('cursor-not-allowed') || best.b.disabled
  };
}
"""

# Buttons inside a screen region (x0, y0, x1, y1), sorted left to right.
REGION_BUTTONS_JS = """
([x0, y0, x1, y1]) => {
  const out = [];
  document.querySelectorAll('button, [role="button"]').forEach(b => {
    const r = b.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < x0 || cx > x1 || cy < y0 || cy > y1) return;
    out.push({x: cx, y: cy, left: r.left,
              label: (b.getAttribute('aria-label') || b.title || b.innerText || '').trim().slice(0, 40)});
  });
  out.sort((a, b) => a.left - b.left);
  return out;
}
"""

UPLOAD_MENU_RE = re.compile(r"upload|local file|from (your )?(device|computer)|browse|my computer|device", re.I)
LIMIT_RE = re.compile(r"concurrent task limit|active sessions allowed", re.I)
LIMIT_ERR = "concurrent task limit reached (this account already has a task running)"
VIDEO_EXTS = (".mp4", ".webm", ".mov")

LOG_FILE = None     # set from --log-file
VERBOSE = False     # set from --verbose


# ---------------------------------------------------------------- logging
def log(m, level="INFO"):
    """Timestamped line to stderr (the UI shows it live) and to the log file."""
    for part in (str(m).splitlines() or [""]):
        line = f"{time.strftime('%H:%M:%S')} [{level}] {part}"
        print(line, file=sys.stderr, flush=True)
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass


def vlog(m):
    """Only with --verbose."""
    if VERBOSE:
        log(m, "DEBUG")


def snap(page, path):
    """Screenshot that logs where it went and never raises."""
    try:
        page.screenshot(path=str(path))
        log(f"screenshot saved: {path}")
        return True
    except Exception as e:
        log(f"screenshot failed ({path}): {e}", "WARN")
        return False


def fail(m):
    log(f"FAILED: {json.dumps(m) if not isinstance(m, str) else m}", "ERROR")
    print(json.dumps({"ok": False, "error": m}), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- helpers
def current_srcs(page, net_videos):
    srcs = []
    for fr in page.frames:          # includes iframes
        try:
            srcs += fr.evaluate(ALL_SRC_JS)
        except Exception:
            pass
    srcs += net_videos              # URLs seen on the network
    return list(dict.fromkeys(srcs))


def dedupe_urls(urls):
    """The same file is served from several mirrors (s3 / wasabi) with the same path.
    Keep one URL per file."""
    seen, out = set(), []
    for u in urls:
        key = u if u.startswith("blob:") else urlparse(u).path
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def dismiss_popups(page):
    for name in ["Not now", "Close", "Got it", "Skip"]:
        try:
            b = page.get_by_role("button", name=name, exact=True).first
            if b.is_visible():
                b.click(timeout=2000)
                log(f"closed popup: {name}")
                page.wait_for_timeout(500)
        except Exception:
            pass


def limit_modal_visible(page):
    """True if the 'Concurrent task limit reached' popup is on screen."""
    try:
        return page.get_by_text(LIMIT_RE).first.is_visible()
    except Exception:
        return False


def choose_skill(page):
    """Open Skill -> Movie Maker; falls back to the 'Movie' chip. True on success."""
    try:
        openers = [
            ("button[aria-label=Select skill]", page.locator('button[aria-label="Select skill"]')),
            ("button name open/select skill", page.get_by_role("button", name=re.compile(r"open skill|select skill", re.I))),
            ("text 'Open Skill'", page.get_by_text("Open Skill", exact=True)),
        ]
        clicked = False
        for label, loc in openers:
            try:
                loc.first.click(timeout=5000)
                clicked = True
                log(f"skill opener worked: {label}")
                break
            except Exception as e:
                log(f"skill opener failed: {label} ({str(e).splitlines()[0]})", "WARN")
                continue
        if not clicked:
            raise RuntimeError("Open Skill button not found")
        log("clicked Open Skill")
        page.wait_for_timeout(1000)
        page.get_by_text(SKILL_NAME, exact=True).first.click(timeout=10000)
        log(f"selected skill: {SKILL_NAME}")
        return True
    except Exception as e:
        log(f"skill flow failed ({e}); trying the '{MODE_TEXT}' chip", "WARN")
    try:
        page.get_by_role("button", name=MODE_TEXT, exact=True).first.click(timeout=10000)
        log("selected Movie chip")
        return True
    except Exception as e:
        log(f"Movie chip failed: {e}", "WARN")
        return False


def _try_chooser(page, click_fn, image_path, wait_ms=4000):
    """Run click_fn(); if a native file chooser opens, answer it with the path."""
    try:
        with page.expect_file_chooser(timeout=wait_ms) as fc:
            click_fn()
        fc.value.set_files(image_path)
        return True
    except Exception:
        return False


def _composer_buttons(page):
    """Buttons to the left of 'Open Skill' on the same row (the '+' attach button is first)."""
    ref = None
    for loc in (page.locator('button[aria-label="Select skill"]').first,
                page.get_by_text("Open Skill", exact=True).first):
        try:
            bb = loc.bounding_box(timeout=2000)
            if bb:
                ref = bb
                break
        except Exception:
            pass
    if ref:
        cy = ref["y"] + ref["height"] / 2
        region = [ref["x"] - 500, cy - 16, ref["x"] - 1, cy + 16]
    else:
        try:
            bb = page.locator(PROMPT_SELECTOR).first.bounding_box(timeout=2000)
        except Exception:
            bb = None
        if not bb:
            return []
        region = [bb["x"], bb["y"], bb["x"] + bb["width"], bb["y"] + bb["height"] + 90]
    return page.evaluate(REGION_BUTTONS_JS, region)


def upload_image(page, image_path):
    """Attach a local image by path: click '+', answer the file chooser. True on success."""
    sp = str(Path(image_path).resolve())
    log(f"attaching image: {sp} ({Path(sp).stat().st_size} bytes)")

    buttons = _composer_buttons(page)
    log(f"attach candidates: {[b['label'] or '(icon)' for b in buttons[:4]]}")
    for btn in buttons[:2]:
        # the '+' opens the file chooser directly...
        if _try_chooser(page, lambda b=btn: page.mouse.click(b["x"], b["y"]), sp):
            log("image attached via file chooser")
            return True
        # ...or opens a small menu with an 'Upload' entry
        try:
            menu = page.locator('[role="menu"], [role="dialog"], [role="listbox"], '
                                '[data-radix-popper-content-wrapper]')
            item = menu.get_by_text(UPLOAD_MENU_RE).first
            if item.is_visible():
                if _try_chooser(page, lambda i=item: i.click(timeout=2000), sp):
                    log("image attached via upload menu")
                    return True
        except Exception:
            pass
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    # last resort: a file input that is now in the page
    try:
        inp = page.locator('input[type="file"]')
        log(f"file inputs on page: {inp.count()}")
        for i in range(inp.count()):
            inp.nth(i).set_input_files(sp)
            log("image attached via file input")
            return True
    except Exception as e:
        log(f"file input route failed: {e}", "WARN")
    return False


def upload_images(page, paths):
    """Attach each image in turn. True only if all of them were attached."""
    for i, p in enumerate(paths, 1):
        log(f"reference image {i}/{len(paths)}")
        if not upload_image(page, p):
            log(f"could not attach image {i}/{len(paths)}: {p}", "ERROR")
            return False
        page.wait_for_timeout(2500)      # let the thumbnail appear before the next one
    return True


def read_credits(page, wait_s=10, stable_reads=2):
    """Return the credit balance as int, or None if it can't be read.

    The value must be identical on `stable_reads` consecutive reads (1.5 s apart),
    so a placeholder shown while the page is still loading is not accepted.
    """
    end = time.time() + wait_s
    last, count = None, 0
    while time.time() < end:
        try:
            n = page.evaluate(CREDITS_JS)
        except Exception:
            n = None
        vlog(f"credit read: {n}")
        if n is None:
            last, count = None, 0
        elif n == last:
            count += 1
            if count >= stable_reads:
                return n
        else:
            last, count = n, 1
        page.wait_for_timeout(1500)
    return None


def save_video(ctx, page, src, path):
    log(f"downloading: {src[:140]}")
    if src.startswith("blob:"):
        data = base64.b64decode(page.evaluate(BLOB_JS, src))
    else:
        r = ctx.request.get(src)
        log(f"download HTTP {r.status}, content-type {r.headers.get('content-type')}")
        if not r.ok:
            raise RuntimeError(f"download status {r.status}")
        data = r.body()
    path.write_bytes(data)
    log(f"saved {path} ({len(data)} bytes)")


# ---------------------------------------------------------------- finding the video
def try_play_clicks(page):
    """Some players only load the <video> after the preview is clicked."""
    candidates = [
        ("video element", page.locator("video")),
        ("button named Play", page.get_by_role("button", name=re.compile(r"\bplay\b", re.I))),
    ]
    for label, loc in candidates:
        try:
            el = loc.first
            if el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(3000)
                log(f"clicked a play/preview element: {label}")
                return True
        except Exception as e:
            vlog(f"play click ({label}) failed: {str(e).splitlines()[0]}")
    log("no play/preview element to click")
    return False


def try_download_button(page, outdir, stamp):
    """Click visible 'Download' buttons/links and capture the browser download."""
    locs = [
        ("button Download", page.get_by_role("button", name=re.compile(r"download", re.I))),
        ("link Download", page.get_by_role("link", name=re.compile(r"download", re.I))),
        ("aria-label/title Download", page.locator('[aria-label*="ownload" i], [title*="ownload" i]')),
    ]
    saved = []
    for label, loc in locs:
        try:
            n = min(loc.count(), 3)
        except Exception:
            continue
        log(f"download candidates ({label}): {n}")
        for i in range(n):
            try:
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                with page.expect_download(timeout=10000) as dl:
                    el.click(timeout=3000)
                d = dl.value
                ext = Path(d.suggested_filename or "").suffix.lower()
                log(f"browser download started: {d.suggested_filename}")
                if ext not in VIDEO_EXTS:
                    log(f"skipping non-video download ({ext or 'no extension'})")
                    continue
                path = outdir / f"movie_{stamp}_dl{len(saved) + 1}{ext}"
                d.save_as(str(path))
                saved.append(str(path))
                log(f"downloaded via button: {path.name}")
            except Exception as e:
                vlog(f"download click failed: {str(e).splitlines()[0]}")
                continue
        if saved:
            break
    return saved


def duration(path):
    """Video length in seconds (needs ffprobe), or None."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def pick_final(parts, want_s, stamp, outdir):
    """Return the finished video.

    Normally the agent's own joined video is the longest clip. If only short clips
    were produced (join step missing or failed) and ffmpeg is available, the clips
    are joined here.
    """
    durs = {p: duration(p) for p in parts}
    log("clip lengths: " + ", ".join(
        f"{p.name}={'?' if durs[p] is None else round(durs[p], 1)}s" for p in parts))
    longest = max(parts, key=lambda p: durs[p] or 0) if any(durs.values()) else parts[-1]
    have = durs.get(longest) or 0
    final = outdir / f"movie_{stamp}_final.mp4"

    if want_s and len(parts) > 1 and have and have < want_s - 2:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            log(f"longest clip is {have:.0f}s but {want_s}s were requested: joining {len(parts)} clips")
            cmd = [ffmpeg, "-y", "-loglevel", "error"]
            for p in parts:
                cmd += ["-i", str(p)]
            cmd += ["-filter_complex",
                    "".join(f"[{i}:v]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(final)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and final.exists():
                log(f"joined video ready: {final.name}")
                return final
            log(f"join failed: {r.stderr.strip()[:300]}", "ERROR")
        else:
            log(f"only {have:.0f}s of {want_s}s found and ffmpeg is missing, so the clips cannot be "
                f"joined (install it: winget install Gyan.FFmpeg)", "WARN")
    shutil.copy(longest, final)
    return final


def to_webm(mp4_path):
    """Re-encode to WebM (VP9/Opus) so VS Code's built-in player can play it."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("ffmpeg not found; skipping WebM conversion", "WARN")
        return None
    webm = mp4_path.with_suffix(".webm")
    log(f"converting to WebM: {webm.name}")
    r = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(mp4_path),
         "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
         "-c:a", "libopus", str(webm)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not webm.exists():
        log(f"ffmpeg failed: {r.stderr.strip()[:300]}", "ERROR")
        return None
    log(f"WebM ready ({webm.stat().st_size} bytes)")
    return webm


def open_in_vscode(path):
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        log("'code' command not found. In VS Code run: "
            "Ctrl+Shift+P -> 'Shell Command: Install code command in PATH'", "WARN")
        return False
    subprocess.run([code, "-r", str(Path(path).resolve())], check=False)
    log(f"opened in VS Code: {path}")
    return True


def expand_cookie_args(items):
    """Accept cookie files and/or folders (a folder = all *.json inside, sorted)."""
    out = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            out += [str(f) for f in sorted(p.glob("*.json"))]
        else:
            out.append(str(p))
    return out


def fmt_elapsed(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- one account
def run_account(browser, cookie_file, args):
    """Try one account.

    Returns (result_dict, None, submitted) on success, or (None, error_text, submitted).
    `submitted` is True once the movie request has been sent on this account. After that
    point the caller must NOT fall back to another account, or a second video would be
    generated (and paid for) on a different account.
    """
    tag = Path(cookie_file).stem
    outdir = Path(args.outdir)
    state = {"submitted": False}
    t_account = time.time()

    if not Path(cookie_file).exists():
        return None, "cookie file not found", False

    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    log("browser context created (viewport 1400x900)")
    page = None
    try:
        cookies = load_cookies(cookie_file)
        ctx.add_cookies(cookies)
        log(f"loaded {len(cookies)} cookies from {cookie_file}")
        page = ctx.new_page()

        net_videos = []             # video URLs seen as network responses
        api_videos = []             # video URLs found inside JSON responses after submit
        seen_resp = []              # response log after submit (for debugging)

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                u = resp.url
                vlog(f"HTTP {resp.status} {ct.split(';')[0] or '-'} {u[:160]}")
                if ct.startswith("video/") or re.search(r"\.(mp4|webm|mov)(\?|$)", u, re.I):
                    if u not in net_videos:
                        net_videos.append(u)
                        log(f"network video: {u[:160]}")
                    return
                if not state["submitted"]:
                    return
                if len(seen_resp) < 300:
                    seen_resp.append(f"{resp.status} {ct.split(';')[0]} {u[:150]}")
                if ("json" in ct or ct.startswith("text/plain")) and "event-stream" not in ct:
                    body = resp.text()
                    if len(body) > 3_000_000:
                        return
                    for m in VIDEO_URL_RE.findall(body.replace("\\/", "/")):
                        if m not in api_videos:
                            api_videos.append(m)
                            log(f"video url in API response: {m[:160]}")
            except Exception:
                pass

        def on_console(msg):
            vlog(f"console.{msg.type}: {msg.text[:200]}")

        def on_pageerror(err):
            log(f"page error: {str(err)[:300]}", "WARN")

        def on_reqfailed(req):
            try:
                vlog(f"request failed: {req.url[:140]} ({req.failure})")
            except Exception:
                pass

        page.on("response", on_response)
        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("requestfailed", on_reqfailed)

        log(f"opening {AGENT_URL}")
        page.goto(AGENT_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('div[role="textbox"], span.tabular-nums', timeout=15000)
            log("page ready (prompt box or credits badge found)")
        except Exception:
            log("page ready-selector not found within 15 s; continuing", "WARN")
        page.wait_for_timeout(1500)
        dismiss_popups(page)
        log(f"at {page.url}")

        # 0) credit check -> skip this account if empty or unreadable
        credits = read_credits(page)
        log(f"credits: {credits}")
        if credits is None:
            snap(page, f"debug_{tag}.png")
            try:
                Path(f"debug_topbar_{tag}.txt").write_text(page.evaluate(TOP_DUMP_JS), encoding="utf-8")
                log(f"top bar dump saved: debug_topbar_{tag}.txt")
            except Exception:
                pass
            if args.allow_unknown_credits:
                log("could not read credits; continuing because --allow-unknown-credits is set", "WARN")
            else:
                return None, f"could not read credits (cookies expired or page not loaded?) - see debug_{tag}.png", False
        elif credits < args.min_credits:
            return None, f"not enough credits ({credits} < {args.min_credits})", False
        else:
            log(f"credits OK ({credits} >= {args.min_credits})")

        # 0b) attach the reference images (one or several)
        if args.image:
            missing = [p for p in args.image if not Path(p).exists()]
            if missing:
                return None, f"image not found: {missing[0]}", False
            if not upload_images(page, args.image):
                snap(page, f"debug_{tag}.png")
                return None, "could not attach the reference images", False
            page.wait_for_timeout(4000)
            snap(page, f"after_upload_{tag}.png")
        else:
            log("no reference image")

        # 1) Open Skill -> Movie Maker (falls back to the "Movie" chip)
        if not choose_skill(page):
            snap(page, f"debug_{tag}.png")
            return None, "could not select the Movie Maker skill or Movie mode", False
        page.wait_for_timeout(2000)
        snap(page, f"after_skill_{tag}.png")

        # 2) type the prompt
        box = page.locator(PROMPT_SELECTOR).first
        try:
            box.wait_for(state="visible", timeout=15000)
            box.click(timeout=10000)
        except Exception as e:
            log(f"prompt box problem: {str(e).splitlines()[0]}", "WARN")
            dismiss_popups(page)
            snap(page, f"debug_{tag}.png")
            return None, "prompt box not clickable", False
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.insert_text(args.prompt)
        log(f"prompt typed ({len(args.prompt)} chars)")
        page.wait_for_timeout(1000)

        before = set(current_srcs(page, net_videos))
        log(f"video URLs already on the page before sending: {len(before)}")

        # 3) send  -- from here on the request may be running on THIS account
        state["submitted"] = True
        sent = False
        for attempt in range(1, 21):
            pos = page.evaluate(SEND_POS_JS)
            vlog(f"send button probe {attempt}: {pos}")
            if pos and not pos["disabled"]:
                page.mouse.click(pos["x"], pos["y"])
                sent = True
                log(f"clicked send button at ({int(pos['x'])}, {int(pos['y'])})")
                break
            page.wait_for_timeout(500)
        if not sent:
            log("send button not found/enabled; pressing Enter instead", "WARN")
            page.keyboard.press("Enter")
        log("submitted; waiting for the movie...")
        page.wait_for_timeout(3000)
        log(f"page url after submit: {page.url}")

        # the site refuses the request when this account already has a running task;
        # nothing was generated, so it is safe to move on to the next account
        for _ in range(8):
            if limit_modal_visible(page):
                log("concurrent task limit popup detected", "WARN")
                snap(page, f"debug_{tag}.png")
                for name in ("Cancel", "Close"):
                    try:
                        page.get_by_role("button", name=name, exact=True).first.click(timeout=1500)
                        break
                    except Exception:
                        pass
                return None, LIMIT_ERR, False
            page.wait_for_timeout(1000)

        # 4) wait until the AGENT is finished (not just until the first video shows up).
        #    A long video is built from several 10 s clips + a final join, so the first
        #    video that appears is only a part of the result.
        found, done_at, idle_since = [], None, None
        start = time.time()
        timeout_s = args.timeout_min * 60
        last_text, last_shot, last_print = "", start, 0
        while time.time() - start < timeout_s:
            now = current_srcs(page, net_videos)
            new = [s for s in now if s not in before and s not in found]
            if new:
                found.extend(new)
                for s in new:
                    log(f"new video URL: {s[:160]}")

            body = ""
            try:
                body = page.inner_text("body")
            except Exception:
                pass

            vids = dedupe_urls(found + api_videos)

            if time.time() - last_print > 15:
                tail = body[-500:].replace("\n", " ")
                log(f"waiting {fmt_elapsed(time.time() - start)}: distinct videos={len(vids)}, "
                    f"network={len(net_videos)}, api={len(api_videos)}")
                if tail and tail != last_text:
                    log(f"page: ...{tail}")
                    last_text = tail
                last_print = time.time()

            if time.time() - last_shot > 120:
                snap(page, f"progress_{tag}.png")
                last_shot = time.time()

            if not vids and LIMIT_RE.search(body):
                log("concurrent task limit text found on the page", "WARN")
                return None, LIMIT_ERR, False

            # site refused the request for lack of credits -> nothing was generated,
            # so it is safe to try the next account
            if not vids and re.search(r"insufficient|not enough (credits|meme)|out of credits", body, re.I):
                log("site reports insufficient credits", "WARN")
                return None, "site reports insufficient credits", False

            if done_at is None and "This round consumed" in body:
                done_at = time.time()
                log(f"agent finished after {fmt_elapsed(done_at - start)} (credits message seen)")

            if BUSY_RE.search(body):
                idle_since = None
            elif idle_since is None:
                idle_since = time.time()

            t = time.time()
            if vids and ((done_at and t - done_at > 15) or (idle_since and t - idle_since > IDLE_DONE_S)):
                log(f"agent finished; {len(vids)} distinct video(s) found")
                break
            if not vids and done_at and t - done_at > POST_DONE_WAIT_S:
                log(f"no video detected {POST_DONE_WAIT_S} s after the agent finished", "WARN")
                break
            page.wait_for_timeout(3000)
        else:
            log(f"gave up waiting after {fmt_elapsed(timeout_s)}", "WARN")

        stamp = time.strftime("%Y%m%d_%H%M%S")
        downloaded = []

        # screenshot of the finished page (fixed name: overwritten every run)
        snap(page, outdir / "final.png")

        # 4b) fallbacks when the agent finished but no video URL was detected
        videos = dedupe_urls(found + api_videos)
        log(f"distinct video URLs: {len(videos)}")
        if not videos:
            log("no video URL detected; trying fallbacks")
            if try_play_clicks(page):
                videos = dedupe_urls([s for s in current_srcs(page, net_videos) if s not in before])
                log(f"after play click: {len(videos)} video URL(s)")
        if not videos:
            downloaded = try_download_button(page, outdir, stamp)
        if not videos and not downloaded and page.url.rstrip("/") != AGENT_URL.rstrip("/"):
            log("reloading the conversation page to look for the video")
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                videos = dedupe_urls([s for s in current_srcs(page, net_videos) if s not in before])
                log(f"after reload: {len(videos)} video URL(s)")
            except Exception as e:
                log(f"reload failed: {e}", "WARN")

        if not videos and not downloaded:
            snap(page, f"debug_{tag}.png")
            try:
                Path(f"debug_page_{tag}.html").write_text(page.content(), encoding="utf-8")
                log(f"page HTML saved: debug_page_{tag}.html")
            except Exception:
                pass
            dump = {"url": page.url,
                    "page_srcs": current_srcs(page, []),
                    "network_videos": net_videos,
                    "api_videos": api_videos,
                    "responses_after_submit": seen_resp,
                    "html_tail": page.content()[-4000:]}
            Path(f"debug_media_{tag}.json").write_text(json.dumps(dump, indent=2), encoding="utf-8")
            log(f"media debug saved: debug_media_{tag}.json")
            return None, (f"no video found (see debug_{tag}.png, debug_page_{tag}.html and "
                          f"debug_media_{tag}.json; the video may still be in that account's history)"), True

        credits_after = read_credits(page, wait_s=8)
        log(f"credits left: {credits_after}")

        # 5) save every distinct clip, then choose (or build) the final video
        parts, seen_hash = [], set()
        for i, src in enumerate(videos, 1):
            path = outdir / f"movie_{stamp}_part{i}.mp4"
            save_video(ctx, page, src, path)
            h = hashlib.sha1(path.read_bytes()).hexdigest()
            if h in seen_hash:
                log(f"part {i} is identical to an earlier one; removed")
                path.unlink()
                continue
            seen_hash.add(h)
            parts.append(path)
        parts += [Path(p) for p in downloaded]

        final = pick_final(parts, args.seconds, stamp, outdir)
        files = [str(final)] + [str(p) for p in parts]

        if not args.no_open:
            to_open = final
            if not args.no_webm:
                webm = to_webm(final)
                if webm:
                    files.append(str(webm))
                    to_open = webm
            open_in_vscode(to_open)
        else:
            log("--no-open set: not opening VS Code")

        log(f"account finished in {fmt_elapsed(time.time() - t_account)}; {len(files)} file(s)")
        return {"ok": True, "prompt": args.prompt, "cookie_file": cookie_file,
                "credits_before": credits, "credits_after": credits_after,
                "files": files}, None, True

    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        if page is not None:
            snap(page, f"debug_{tag}.png")
        return None, f"timeout: {str(e).splitlines()[0]}", state["submitted"]
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}", state["submitted"]
    finally:
        ctx.close()
        log("browser context closed")


def main():
    global LOG_FILE, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--outdir", default="outputs")
    ap.add_argument("--cookies", nargs="+", default=["digen_cookies.json"],
                    help="one or more cookie files, or a folder of .json files; "
                         "tried in order until one has credits")
    ap.add_argument("--min-credits", type=int, default=50,
                    help="skip an account if its credits are below this (a 10 s clip "
                         "used 50 credits in your log)")
    ap.add_argument("--allow-unknown-credits", action="store_true",
                    help="continue even if the credit balance cannot be read "
                         "(default: skip that account)")
    ap.add_argument("--image", nargs="+", default=None,
                    help="one or more reference images to attach before sending")
    ap.add_argument("--seconds", type=int, default=0,
                    help="requested video length; if only shorter clips come back "
                         "they are joined with ffmpeg")
    ap.add_argument("--timeout-min", type=int, default=75,
                    help="maximum minutes to wait for the agent to finish")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="do not open the videos in VS Code")
    ap.add_argument("--no-webm", action="store_true", help="do not convert to WebM before opening")
    ap.add_argument("--log-file", default=None, help="also write every log line to this file")
    ap.add_argument("--verbose", action="store_true",
                    help="log every network response, console message and failed request")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    log("==== run started ====")
    log(f"python {platform.python_version()} on {platform.platform()}")
    try:
        import playwright
        log(f"playwright {getattr(playwright, '__version__', '?')}")
    except Exception:
        pass
    log(f"ffmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}; ffprobe: {shutil.which('ffprobe') or 'NOT FOUND'}")
    log(f"prompt: {args.prompt}")
    log(f"options: outdir={args.outdir} min_credits={args.min_credits} "
        f"images={len(args.image) if args.image else 0} "
        f"seconds={args.seconds} timeout_min={args.timeout_min} "
        f"headed={args.headed} no_open={args.no_open} no_webm={args.no_webm} "
        f"allow_unknown_credits={args.allow_unknown_credits} verbose={VERBOSE}")
    log(f"log file: {LOG_FILE or '(none)'}")

    cookie_files = expand_cookie_args(args.cookies)
    log(f"cookie files ({len(cookie_files)}): {cookie_files}")
    if not cookie_files:
        fail("No cookie files found")
    Path(args.outdir).mkdir(exist_ok=True)

    errors = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        log(f"chromium launched (headless={not args.headed})")
        try:
            for n, cf in enumerate(cookie_files, 1):
                log(f"=== account {n}/{len(cookie_files)}: {cf} ===")
                result, err, submitted = run_account(browser, cf, args)
                if result:
                    Path("result.json").write_text(json.dumps(result, indent=2))
                    log(f"SUCCESS on {cf}; total time {fmt_elapsed(time.time() - t0)}")
                    log(f"result: {json.dumps(result)}")
                    print(json.dumps(result), flush=True)
                    return
                errors[cf] = err
                if submitted:
                    # The request was already sent on this account. Moving on would
                    # generate a second video on a different account, so stop here.
                    log(f"{cf}: {err} -> request was already submitted; NOT trying other accounts", "ERROR")
                    fail({"message": f"stopped: the request was already submitted on {cf}, so no other "
                                     f"account was tried (to avoid generating a second video). "
                                     f"Check that account's history on the site.",
                          "details": errors})
                log(f"{cf}: {err} -> trying next cookie", "WARN")
        finally:
            browser.close()
            log("browser closed")

    fail({"message": "all cookie files failed", "details": errors})


if __name__ == "__main__":
    main()