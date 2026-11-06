"""Google Flow worker (flow.google.com).

Attaches reference images, sends the prompt, approves the generation, waits for the
video and saves it. Accounts are cookie files (EditThisCookie / Playwright JSON export).

  python flow_work.py --check cookies_flow/acc1.json ...   prints {"checked": {"acc1.json": true}}
  python flow_work.py --prompt "..." --cookies cookies_flow/acc1.json [--image a.jpg b.png]
"""
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

from flow_cookies import has_login, load_cookies

FLOW_URL = "https://flow.google.com"
PROMPT_SEL = 'div[role="textbox"][aria-label="What do you want to create?"]'
ADD_BTN_SEL = 'button[aria-label="Add ingredients to the prompt box"]'
SEND_SEL = 'button:has(mat-icon:text-is("arrow_forward")):visible'
CHAR_PROMPT_SEL = 'div[role="textbox"][aria-label^="Describe your character"]'
APPROVE_SEL = 'div[role="radio"][aria-label="Approve"]'
ALWAYS_SEL = 'div[role="radio"][aria-label="Always approve"]'
REJECT_SEL = 'div[role="radio"][aria-label="Reject"]'

COST_RE = re.compile(r"costing\s+([\d,]+)\s+credits?", re.I)
BUSY_RE = re.compile(r"tune\s+stop\b")          # the send arrow turns into a stop icon while the agent works
QUEUE_RE = re.compile(r"in the queue|been scheduled|started generating|check back|will be ready", re.I)
ERR_RE = re.compile(r"something went wrong|couldn't generate|failed to generate|can't generate|violates", re.I)
VIDEO_EXTS = (".mp4", ".webm", ".mov")

ALL_SRC_JS = r"""
() => {
  const out = [];
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (el.tagName === 'VIDEO') {
        if (el.currentSrc) out.push(el.currentSrc);
        if (el.getAttribute('src')) out.push(abs(el.getAttribute('src')));
      }
      if (el.tagName === 'SOURCE' && el.getAttribute('src')) out.push(abs(el.getAttribute('src')));
    });
  };
  walk(document);
  return out.filter(Boolean);
}
"""

IMG_JS = """
(minW) => Array.from(document.images)
  .filter(i => i.complete && i.naturalWidth >= minW && i.currentSrc)
  .map(i => i.currentSrc)
"""

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

LOG_FILE = None
VERBOSE = False


# ---------------------------------------------------------------- logging
def log(m, level="INFO"):
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
    if VERBOSE:
        log(m, "DEBUG")


def snap(page, path):
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


def fmt_elapsed(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- browser / cookies
def start_browser(p, headed):
    kw = dict(headless=not headed, args=["--disable-blink-features=AutomationControlled"],
              ignore_default_args=["--enable-automation"])
    try:
        b = p.chromium.launch(channel="chrome", **kw)
        log("launched installed Google Chrome")
    except Exception as e:
        log(f"Chrome not available ({str(e).splitlines()[0]}); using bundled Chromium", "WARN")
        b = p.chromium.launch(**kw)
    return b


def new_context(browser, cookie_file):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    cookies = load_cookies(cookie_file)
    if not has_login(cookies):
        log("cookie file has no Google login cookies (SID / __Secure-1PSID ...): "
            "export again while signed in, with ALL cookies for google.com", "WARN")
    ctx.add_cookies(cookies)
    log(f"loaded {len(cookies)} cookies from {cookie_file}")
    return ctx


def signed_in(page):
    """True when Flow shows its home or a project (not a sign-in page)."""
    if "accounts.google.com" in page.url:
        return False
    if "/project/" in page.url:
        return True
    try:
        return page.get_by_role("button", name=re.compile(r"new project", re.I)).count() > 0
    except Exception:
        return False


def check_mode(cookie_files, headed=False):
    res = {}
    with sync_playwright() as p:
        browser = start_browser(p, headed)
        for cf in cookie_files:
            name = Path(cf).name
            try:
                ctx = new_context(browser, cf)
                page = ctx.new_page()
                page.goto(FLOW_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(6000)
                res[name] = signed_in(page)
                log(f"{name}: {'signed in' if res[name] else 'NOT signed in (cookies expired or rejected)'}")
                ctx.close()
            except Exception as e:
                log(f"{name}: check failed ({e})", "ERROR")
                res[name] = False
        browser.close()
    print(json.dumps({"checked": res}), flush=True)


# ---------------------------------------------------------------- page helpers
def current_srcs(page, net_videos):
    srcs = []
    for fr in page.frames:
        try:
            srcs += fr.evaluate(ALL_SRC_JS)
        except Exception:
            pass
    srcs += net_videos
    return list(dict.fromkeys(srcs))


def dedupe_urls(urls):
    seen, out = set(), []
    for u in urls:
        key = u if u.startswith("blob:") else urlparse(u).path
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def click_agree(page):
    """'Rights to use this image' dialog. Safe to call any time."""
    try:
        b = page.get_by_role("button", name="I agree", exact=True)
        if b.count() and b.first.is_visible():
            b.first.click(timeout=3000)
            log("clicked 'I agree' (rights dialog)")
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass
    return False


def _click_robust(page, loc, what):
    """Normal click, then forced click, then a DOM click. True on success."""
    for mode in ("normal", "force", "dom"):
        try:
            if mode == "normal":
                loc.click(timeout=4000)
            elif mode == "force":
                loc.click(timeout=4000, force=True)
            else:
                loc.evaluate("el => el.click()", timeout=4000)
            log(f"clicked {what} ({mode})")
            return True
        except Exception as e:
            log(f"click {what} failed ({mode}): {str(e).splitlines()[0]}", "WARN")
    return False


def _upload_locator(page):
    """The 'Upload media' button in the asset picker (several ways to find it)."""
    cands = [
        page.get_by_role("button", name=re.compile(r"upload media", re.I)),
        page.locator("button:has(span.upload-text)"),
        page.locator("button:has-text('Upload media')"),
        page.locator("span.upload-text"),
        page.get_by_text(re.compile(r"^\s*Upload media\s*$", re.I)),
    ]
    for loc in cands:
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def attach_image(page, path, state):
    """+ -> Upload media -> choose file -> (I agree) -> pick the asset -> Add to prompt."""
    sp = str(Path(path).resolve())
    name = Path(sp).name
    log(f"attaching image: {sp} ({Path(sp).stat().st_size} bytes)")
    state["pending"], state["chosen"] = sp, False

    # 1) open the asset picker with the "+" button
    plus = page.locator(ADD_BTN_SEL + ":visible")
    if not plus.count():
        plus = page.locator("button:has(mat-icon.add-menu-icon):visible")
    log(f"'+' buttons found: {plus.count()}")
    if not plus.count() or not _click_robust(page, plus.first, "'+' button"):
        snap(page, "debug_flow_upload.png")
        return False

    # 2) wait for 'Upload media' (click '+' once more if the picker did not open)
    up = None
    for attempt in range(3):
        for _ in range(12):
            click_agree(page)
            up = _upload_locator(page)
            if up:
                break
            page.wait_for_timeout(500)
        if up:
            break
        log(f"'Upload media' not visible (attempt {attempt + 1}); clicking '+' again", "WARN")
        _click_robust(page, plus.first, "'+' button")
    if not up:
        log("'Upload media' never appeared", "ERROR")
        snap(page, "debug_flow_upload.png")
        return False
    log("'Upload media' is visible")

    # 3) click it; the file chooser handler (set up in run_account) answers with the path
    _click_robust(page, up, "'Upload media'")

    end = time.time() + 30
    tried_input = False
    while time.time() < end and not state["chosen"]:
        if click_agree(page):
            # the dialog may have swallowed the first click: press Upload media again
            up2 = _upload_locator(page)
            if up2 and not state["chosen"]:
                page.wait_for_timeout(1000)
                if not state["chosen"]:
                    _click_robust(page, up2, "'Upload media' (after I agree)")
        if not state["chosen"] and not tried_input and time.time() > end - 22:
            tried_input = True
            # fallback: a hidden <input type=file> in the page
            try:
                inp = page.locator('input[type="file"]')
                n = inp.count()
                log(f"file inputs in page: {n}")
                if n:
                    inp.last.set_input_files(sp)
                    state["chosen"] = True
                    log("file set through the file input")
            except Exception as e:
                log(f"file input fallback failed: {str(e).splitlines()[0]}", "WARN")
        page.wait_for_timeout(500)
    if not state["chosen"]:
        log("file chooser never opened", "ERROR")
        snap(page, "debug_flow_upload.png")
        return False

    add_btn = page.get_by_role("button", name=re.compile(r"add to prompt", re.I)).first
    clicked_item = False
    start = time.time()
    while time.time() - start < 45:
        click_agree(page)
        try:
            if add_btn.is_visible():
                break
        except Exception:
            pass
        if not clicked_item and time.time() - start > 10:
            try:
                page.get_by_text(name, exact=True).first.click(timeout=2000)
                clicked_item = True
                log(f"clicked asset {name}")
            except Exception:
                pass
        page.wait_for_timeout(700)
    else:
        log("'Add to prompt' never appeared", "ERROR")
        snap(page, "debug_flow_upload.png")
        return False

    _click_robust(page, add_btn, "'Add to prompt'")
    page.wait_for_timeout(2500)
    try:
        if add_btn.is_visible():          # picker still open
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception:
        pass
    state["pending"] = None
    return True


def wait_approval(page, timeout_s=90, stop_if=None):
    """Return (approve_row, cost_in_credits), or (None, None) if no approval question appeared.
    stop_if() -> True ends the wait early (the agent already answered without asking)."""
    end = time.time() + timeout_s
    quiet_polls = 0
    while time.time() < end:
        try:
            row = page.locator(APPROVE_SEL)
            if row.count() and row.first.is_visible():
                body = page.inner_text("body")
                m = COST_RE.findall(body)
                return row.first, (int(m[-1].replace(",", "")) if m else None)
            if stop_if and stop_if():
                quiet_polls += 1
                if quiet_polls >= 3:
                    return None, None
            else:
                quiet_polls = 0
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return None, None


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


def try_download_button(page, outdir, stamp):
    """Fallback: click visible Download buttons and capture the browser download."""
    saved = []
    locs = [
        ("button Download", page.get_by_role("button", name=re.compile(r"download", re.I))),
        ("aria-label Download", page.locator('[aria-label*="ownload" i], [title*="ownload" i]')),
    ]
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
                with page.expect_download(timeout=15000) as dl:
                    el.click(timeout=3000)
                d = dl.value
                ext = Path(d.suggested_filename or "").suffix.lower()
                if ext not in VIDEO_EXTS:
                    log(f"skipping non-video download ({ext or 'no extension'})")
                    continue
                path = outdir / f"flow_{stamp}_dl{len(saved) + 1}{ext}"
                d.save_as(str(path))
                saved.append(str(path))
                log(f"downloaded via button: {path.name}")
            except Exception as e:
                vlog(f"download click failed: {str(e).splitlines()[0]}")
        if saved:
            break
    return saved


def duration(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def pick_final(parts, stamp, outdir):
    durs = {p: duration(p) for p in parts}
    log("clip lengths: " + ", ".join(f"{p.name}={'?' if durs[p] is None else round(durs[p], 1)}s" for p in parts))
    if any(durs.values()):
        best = max(parts, key=lambda p: durs[p] or 0)
    else:
        best = max(parts, key=lambda p: p.stat().st_size)
    final = outdir / f"flow_{stamp}_final{best.suffix}"
    shutil.copy(best, final)
    return final


def expand_cookie_args(items):
    """Accept cookie files and/or folders (a folder = all *.json inside, sorted)."""
    out = []
    for it in items:
        p = Path(it)
        out += [str(f) for f in sorted(p.glob("*.json"))] if p.is_dir() else [str(p)]
    return out


def dom_images(page, min_w=300):
    """URLs of the large images currently on the page."""
    out = []
    for fr in page.frames:
        try:
            out += fr.evaluate(IMG_JS, min_w)
        except Exception:
            pass
    return list(dict.fromkeys(out))


def save_image(ctx, page, src, stem):
    """Save an image URL (http, blob or data) as <stem>.<ext>. Returns the Path."""
    if src.startswith("data:"):
        head, b64 = src.split(",", 1)
        ext = ".jpg" if "jpeg" in head else ".webp" if "webp" in head else ".png"
        data = base64.b64decode(b64)
    elif src.startswith("blob:"):
        data, ext = base64.b64decode(page.evaluate(BLOB_JS, src)), ".png"
    else:
        r = ctx.request.get(src)
        if not r.ok:
            raise RuntimeError(f"download status {r.status}")
        ct = (r.headers.get("content-type") or "").lower()
        ext = ".jpg" if "jpeg" in ct else ".webp" if "webp" in ct else ".png"
        data = r.body()
    path = Path(str(stem) + ext)
    path.write_bytes(data)
    log(f"saved {path} ({len(data)} bytes)")
    return path


def run_character(ctx, page, args, state, tag, outdir, t_acc):
    """Characters page of the project: describe a character (optional reference image) and save the images."""
    m = re.match(r"(https://flow\.google\.com/project/[^/?#]+)", page.url)
    if not m:
        snap(page, f"debug_flow_{tag}.png")
        return None, "could not read the project URL", False
    page.goto(m.group(1) + "/character", wait_until="domcontentloaded")
    box = page.locator(CHAR_PROMPT_SEL).first
    try:
        box.wait_for(state="visible", timeout=30000)
    except Exception:
        snap(page, f"debug_flow_{tag}.png")
        return None, "the New character page did not open (prompt box 'Describe your character' not found)", False
    log(f"character page ready: {page.url}")
    page.wait_for_timeout(3000)
    before = set(dom_images(page))
    log(f"large images already on the page: {len(before)}")

    if args.image:
        missing = [x for x in args.image if not Path(x).exists()]
        if missing:
            return None, f"image not found: {missing[0]}", False
        for i, x in enumerate(args.image, 1):
            log(f"reference image {i}/{len(args.image)}")
            if not attach_image(page, x, state):
                snap(page, f"debug_flow_{tag}.png")
                return None, f"could not attach image {i}: {x}", False

    box.click(timeout=10000)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(args.prompt)
    log(f"character prompt typed ({len(args.prompt)} chars)")
    page.wait_for_timeout(1000)

    send = page.locator(SEND_SEL)
    sent = False
    for _ in range(20):
        if send.count() and send.last.is_enabled():
            send.last.click()
            sent = True
            log("clicked send")
            break
        page.wait_for_timeout(500)
    if not sent:
        log("send button not found/enabled; pressing Enter", "WARN")
        page.keyboard.press("Enter")
    state["submitted"] = True

    row, cost = wait_approval(page, timeout_s=15)     # images may not ask for approval
    if row is not None:
        log(f"approval asked; cost: {cost} credits")
        page.locator(ALWAYS_SEL).first.click(timeout=5000) if args.always_approve else row.click(timeout=5000)
        log("approved the generation")

    found, last_change = [], time.time()
    start = time.time()
    timeout_s = args.timeout_min * 60
    last_shot = last_print = 0
    while time.time() - start < timeout_s:
        now = [u for u in dom_images(page) if u not in before]
        if set(now) != set(found):
            found, last_change = now, time.time()
            log(f"new images on the page: {len(found)}")
        if time.time() - last_print > 15:
            log(f"waiting {fmt_elapsed(time.time() - start)}: new images={len(found)}")
            last_print = time.time()
        if time.time() - last_shot > 30:
            snap(page, outdir / "live.png")
            last_shot = time.time()
        if not found:
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            if ERR_RE.search(body):
                snap(page, f"debug_flow_{tag}.png")
                return None, "Flow reported an error while generating (see debug_flow screenshot)", True
        if found and time.time() - last_change > args.char_idle_s:
            log(f"images stable for {args.char_idle_s}s; done")
            break
        page.wait_for_timeout(3000)
    snap(page, outdir / "final.png")
    if not found:
        snap(page, f"debug_flow_{tag}.png")
        return None, f"no new image appeared (see debug_flow_{tag}.png)", True

    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved, seen = [], set()
    for i, src in enumerate(found, 1):
        try:
            path = save_image(ctx, page, src, outdir / f"flow_char_{stamp}_{i}")
        except Exception as e:
            log(f"download of image {i} failed: {e}", "WARN")
            continue
        h = hashlib.sha1(path.read_bytes()).hexdigest()
        if h in seen:
            path.unlink()
            continue
        seen.add(h)
        saved.append(path)
    if not saved:
        return None, "images were found but none could be downloaded", True
    log(f"character done in {fmt_elapsed(time.time() - t_acc)}; {len(saved)} image(s)")
    return {"ok": True, "kind": "character", "prompt": args.prompt, "files": [str(p) for p in saved]}, None, True


# ---------------------------------------------------------------- one account
def run_account(browser, cookie_file, args):
    """Returns (result, None, submitted) or (None, error, submitted).
    After submitted is True the caller must not try another account."""
    tag = Path(cookie_file).stem
    outdir = Path(args.outdir)
    state = {"submitted": False, "pending": None, "chosen": False}
    t_acc = time.time()
    if not Path(cookie_file).exists():
        return None, "cookie file not found", False
    ctx = new_context(browser, cookie_file)
    page = None
    try:
        page = ctx.new_page()
        net_videos = []

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                u = resp.url
                if ct.startswith("video/") or re.search(r"\.(mp4|webm|mov)(\?|$)", u, re.I):
                    if u not in net_videos:
                        net_videos.append(u)
                        log(f"network video: {u[:160]}")
            except Exception:
                pass

        def on_chooser(fc):
            if state["pending"]:
                fc.set_files(state["pending"])
                state["chosen"] = True
                log("file chosen in the file chooser")

        page.on("response", on_response)
        page.on("filechooser", on_chooser)
        page.on("pageerror", lambda e: log(f"page error: {str(e)[:300]}", "WARN"))

        target = args.project_url or FLOW_URL
        log(f"opening {target}")
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        if "accounts.google.com" in page.url:
            snap(page, f"debug_flow_{tag}.png")
            return None, "not signed in: the cookies are expired or Google rejected them (export fresh ones)", False

        if not args.project_url:
            try:
                new = page.get_by_role("button", name=re.compile(r"new project", re.I)).first
                if new.is_visible():
                    new.click(timeout=5000)
                    log("clicked New project")
                    page.wait_for_timeout(4000)
            except Exception as e:
                log(f"New project click failed: {str(e).splitlines()[0]}", "WARN")

        box = page.locator(PROMPT_SEL).first
        try:
            box.wait_for(state="visible", timeout=30000)
        except Exception:
            snap(page, f"debug_flow_{tag}.png")
            return None, "prompt box not found (cookies rejected, or the page layout changed)", False
        log(f"project ready: {page.url}")
        if args.mode == "character":
            return run_character(ctx, page, args, state, tag, outdir, t_acc)

        before = set(current_srcs(page, net_videos))

        # reference images
        if args.image:
            missing = [x for x in args.image if not Path(x).exists()]
            if missing:
                return None, f"image not found: {missing[0]}", False
            for i, x in enumerate(args.image, 1):
                log(f"reference image {i}/{len(args.image)}")
                if not attach_image(page, x, state):
                    snap(page, f"debug_flow_{tag}.png")
                    return None, f"could not attach image {i}: {x}", False
            snap(page, f"after_upload_flow_{tag}.png")

        # prompt
        box.click(timeout=10000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.insert_text(args.prompt)
        log(f"prompt typed ({len(args.prompt)} chars)")
        page.wait_for_timeout(1000)

        # baselines taken right before sending
        imgs_before = set(dom_images(page, 200))
        try:
            thumbs_before = page.inner_text("body").count("thumb_up")
        except Exception:
            thumbs_before = 0

        def agent_replied():
            body_ = page.inner_text("body")
            return (not BUSY_RE.search(body_)) and body_.count("thumb_up") > thumbs_before

        # send
        send = page.locator(SEND_SEL)
        sent = False
        for _ in range(20):
            if send.count() and send.last.is_enabled():
                send.last.click()
                sent = True
                log("clicked send")
                break
            page.wait_for_timeout(500)
        if not sent:
            log("send button not found/enabled; pressing Enter", "WARN")
            page.keyboard.press("Enter")
        state["submitted"] = True

        # approval question (videos ask; images are generated without asking)
        row, cost = wait_approval(page, stop_if=agent_replied)
        if row is not None:
            log(f"approval asked; cost: {cost} credits")
            if cost is not None and args.max_credits and cost > args.max_credits:
                try:
                    page.locator(REJECT_SEL).first.click(timeout=3000)
                except Exception:
                    pass
                return None, f"generation costs {cost} credits (limit {args.max_credits}); rejected", True
            target_row = page.locator(ALWAYS_SEL).first if args.always_approve else row
            target_row.click(timeout=5000)
            log("approved the generation")
            thumbs_before = max(thumbs_before, page.inner_text("body").count("thumb_up"))
        else:
            log("no approval question (the agent answered directly, e.g. images)")
        page.wait_for_timeout(2000)

        # wait until the agent has answered AND its media is on the page
        found, found_imgs = [], []
        agent_done_since = None
        last_media_change = time.time()
        start = time.time()
        timeout_s = args.timeout_min * 60
        last_print = last_shot = 0
        while time.time() - start < timeout_s:
            now = current_srcs(page, net_videos)
            new = [x for x in now if x not in before and x not in found]
            if new:
                found.extend(new)
                last_media_change = time.time()
                for x in new:
                    log(f"new video URL: {x[:160]}")
            imgs = dedupe_urls([u for u in dom_images(page, 200) if u not in imgs_before])
            if set(imgs) != set(found_imgs):
                found_imgs = imgs
                last_media_change = time.time()
                log(f"new images on the page: {len(found_imgs)}")
            body = ""
            try:
                body = page.inner_text("body")
            except Exception:
                pass
            vids = dedupe_urls(found)
            busy = bool(BUSY_RE.search(body))
            replied = body.count("thumb_up") > thumbs_before
            if replied and not busy:
                agent_done_since = agent_done_since or time.time()
            else:
                agent_done_since = None
            queued = bool(QUEUE_RE.search(body[-1500:]))

            if time.time() - last_print > 15:
                log(f"waiting {fmt_elapsed(time.time() - start)}: agent busy={busy}, replied={replied}, "
                    f"videos={len(vids)}, images={len(found_imgs)}, queued={queued}")
                last_print = time.time()
            if time.time() - last_shot > 30:
                snap(page, outdir / "live.png")
                last_shot = time.time()

            if not vids and not found_imgs and ERR_RE.search(body):
                snap(page, f"debug_flow_{tag}.png")
                return None, "Flow reported an error while generating (see debug_flow screenshot)", True

            if agent_done_since:
                quiet = time.time() - last_media_change
                if vids and quiet > args.idle_s:
                    log(f"agent finished; {len(vids)} video(s), {len(found_imgs)} image(s)")
                    break
                if not vids and found_imgs and not queued and quiet > 10:
                    log(f"agent finished; {len(found_imgs)} image(s), no video")
                    break
                if not vids and not found_imgs and not queued and time.time() - agent_done_since > 60:
                    log("agent finished but no media was found", "WARN")
                    break
            page.wait_for_timeout(3000)
        else:
            log(f"gave up waiting after {fmt_elapsed(timeout_s)}", "WARN")

        stamp = time.strftime("%Y%m%d_%H%M%S")
        snap(page, outdir / "final.png")
        videos = dedupe_urls(found)
        downloaded = []
        if not videos and not found_imgs:
            log("no media detected; trying the Download button", "WARN")
            downloaded = try_download_button(page, outdir, stamp)
        if not videos and not found_imgs and not downloaded:
            snap(page, f"debug_flow_{tag}.png")
            Path(f"debug_flow_page_{tag}.html").write_text(page.content(), encoding="utf-8")
            return None, (f"no video or image found (see debug_flow_{tag}.png and debug_flow_page_{tag}.html; "
                          f"the media may still be in that account's project)"), True

        parts, seen_hash = [], set()
        for i, src in enumerate(videos, 1):
            path = outdir / f"flow_{stamp}_part{i}.mp4"
            try:
                save_video(ctx, page, src, path)
            except Exception as e:
                log(f"download of part {i} failed: {e}", "WARN")
                continue
            h = hashlib.sha1(path.read_bytes()).hexdigest()
            if h in seen_hash:
                path.unlink()
                continue
            seen_hash.add(h)
            parts.append(path)
        parts += [Path(x) for x in downloaded]

        images = []
        for i, src in enumerate(found_imgs, 1):
            try:
                path = save_image(ctx, page, src, outdir / f"flow_img_{stamp}_{i}")
            except Exception as e:
                log(f"download of image {i} failed: {e}", "WARN")
                continue
            h = hashlib.sha1(path.read_bytes()).hexdigest()
            if h in seen_hash:
                path.unlink()
                continue
            seen_hash.add(h)
            images.append(path)

        if not parts and not images:
            return None, "media was found but none of it could be downloaded", True
        files = []
        if parts:
            final = pick_final(parts, stamp, outdir)
            files = [str(final)] + [str(x) for x in parts]
        files += [str(x) for x in images]
        kind = "video" if parts else "images"
        log(f"account finished in {fmt_elapsed(time.time() - t_acc)}; {kind}; {len(files)} file(s)")
        return {"ok": True, "kind": kind, "prompt": args.prompt, "cookie_file": str(cookie_file),
                "files": files}, None, True

    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        if page is not None:
            snap(page, f"debug_flow_{tag}.png")
        return None, f"timeout: {str(e).splitlines()[0]}", state["submitted"]
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}", state["submitted"]
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        log("browser context closed")


def main():
    global LOG_FILE, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", nargs="+", metavar="COOKIE_FILE", help="report which cookie files are signed in")
    ap.add_argument("--prompt")
    ap.add_argument("--mode", choices=["video", "image", "character"], default="video",
                    help="video / image = the session chat (the agent decides what it makes); "
                         "character = the New character page")
    ap.add_argument("--char-idle-s", type=int, default=25,
                    help="character mode: seconds with no new images = done")
    ap.add_argument("--outdir", default="outputs_flow")
    ap.add_argument("--cookies", nargs="+", default=[], help="cookie files or a folder of .json files, tried in order")
    ap.add_argument("--image", nargs="+", default=None, help="reference image paths")
    ap.add_argument("--project-url", default=None, help="reuse an existing Flow project instead of a new one")
    ap.add_argument("--max-credits", type=int, default=0, help="reject if the generation costs more (0 = no limit)")
    ap.add_argument("--always-approve", action="store_true")
    ap.add_argument("--idle-s", type=int, default=45, help="seconds without activity after a video appears = done")
    ap.add_argument("--timeout-min", type=int, default=30)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    if args.check:
        return check_mode(expand_cookie_args(args.check), args.headed)
    if not args.prompt:
        fail("--prompt is required")

    t0 = time.time()
    log("==== run started ====")
    log(f"python {platform.python_version()} on {platform.platform()}")
    log(f"ffmpeg: {shutil.which('ffmpeg') or 'NOT FOUND'}; ffprobe: {shutil.which('ffprobe') or 'NOT FOUND'}")
    log(f"prompt: {args.prompt}")
    log(f"options: mode={args.mode} outdir={args.outdir} images={len(args.image) if args.image else 0} "
        f"max_credits={args.max_credits} always_approve={args.always_approve} "
        f"timeout_min={args.timeout_min} headed={args.headed}")

    cookie_files = expand_cookie_args(args.cookies)
    log(f"cookie files ({len(cookie_files)}): {cookie_files}")
    if not cookie_files:
        fail("No cookie files given. Add an account first.")
    Path(args.outdir).mkdir(exist_ok=True)

    errors = {}
    with sync_playwright() as p:
        browser = start_browser(p, args.headed)
        try:
            for n, cf in enumerate(cookie_files, 1):
                log(f"=== account {n}/{len(cookie_files)}: {cf} ===")
                result, err, submitted = run_account(browser, cf, args)
                if result:
                    Path("result_flow.json").write_text(json.dumps(result, indent=2))
                    log(f"SUCCESS on {cf}; total time {fmt_elapsed(time.time() - t0)}")
                    print(json.dumps(result), flush=True)
                    return
                errors[cf] = err
                if submitted:
                    log(f"{cf}: {err} -> request already submitted; NOT trying other accounts", "ERROR")
                    fail({"message": f"stopped: the request was already submitted on {cf}, so no other "
                                     f"account was tried (to avoid a second video). Check that account's project.",
                          "details": errors})
                log(f"{cf}: {err} -> trying next cookie file", "WARN")
        finally:
            browser.close()
            log("browser closed")
    fail({"message": "all cookie files failed", "details": errors})


if __name__ == "__main__":
    main()