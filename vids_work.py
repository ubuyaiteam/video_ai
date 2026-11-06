"""Google Vids video generator worker (same structure as the TAAFT worker).

Opens Google Vids, creates a blank video, opens "Generate a video", fills the prompt, optionally
attaches one reference image, presses Generate, waits for the clip and downloads it.

Google Vids needs a signed-in Google account, so at least one cookie file is required (cookie files
of your own Google account, tried in order; an account that fails before submitting is skipped).
Uses cookie_loader.py when it exists next to this script, otherwise a built-in loader.
The browser is a plain Playwright browser: no profile folders, no stealth flags.

If a step fails it saves debug_vids_<account>.png, debug_vids_controls_<account>.txt and
debug_vids_text_<account>.txt (the visible page text, to see notices or "not available" pages).
"""
import argparse
import base64
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HOME_URL = "https://docs.google.com/videos/"
CREATE_URL = "https://docs.google.com/videos/create"      # fallback only
DL_TIMEOUT_MS = 600_000
IDLE_DONE_S = 25
MAX_IMAGE_MB = 16

SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}

NEW_VIDEO_RE = (r"blank video|^blank$|new video|start (a )?(new|blank)|^create( new)?$|"
                r"start from scratch|^\+$|^new$|create (a )?video")
RATIO_LABEL = {"16:9": "landscape", "21:9": "landscape", "4:3": "landscape", "9:16": "portrait", "1:1": "square"}

BUSY_RE = re.compile(r"generating|creating (your )?video|this (may|can) take|in progress|\d{1,2}%", re.I)
LIMIT_RE = re.compile(r"daily limit|limits? (has |have )?(been )?reached|hit your limits?|you.ve hit|"
                      r"reached your .{0,25}limit|limits? for generating|try again tomorrow|usage limit|quota", re.I)
ERROR_RE = re.compile(r"something went wrong|couldn.t generate|can.t generate|unable to generate|"
                      r"try a different prompt|violates|policy", re.I)
READY_RE = re.compile(r"^(insert|add to (video|scene)|use (this )?video|apply)$", re.I)
POPUP_NAMES = ["Got it", "Not now", "No thanks", "Maybe later", "Dismiss", "Skip", "Close", "Done",
               "Accept all", "I agree"]

ALL_SRC_JS = r"""
() => {
  const out = [];
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  document.querySelectorAll('video, video source').forEach(el => {
    if (el.currentSrc) out.push(el.currentSrc);
    const s = el.getAttribute('src'); if (s) out.push(abs(s));
  });
  return out.filter(Boolean);
}
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
CONTROLS_JS = """
() => Array.from(document.querySelectorAll('button, [role="button"], [role="menuitem"], a, textarea, input, [contenteditable="true"], [role="textbox"]'))
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => e.tagName + ' | ' + (e.getAttribute('role') || '') + ' | aria=' + (e.getAttribute('aria-label') || '')
       + ' | ph=' + (e.getAttribute('placeholder') || '') + ' | ' + (e.innerText || e.value || '').trim().slice(0, 50).replace(/\\n/g, ' '))
  .join('\\n')
"""

LOG_FILE = None
VERBOSE = False


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


def fail(m):
    log(f"FAILED: {json.dumps(m) if not isinstance(m, str) else m}", "ERROR")
    print(json.dumps({"ok": False, "error": m}), flush=True)
    sys.exit(1)


def quiet_snap(page, path, full=False):
    try:
        page.screenshot(path=str(path), full_page=full)
    except Exception:
        pass


def fmt(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- cookies
def _fallback_load_cookies(path):
    """Cookie-extension export (list) or Playwright storage_state ({'cookies': [...]})."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("cookies", [])
    out = []
    for c in data:
        if not c.get("name") or "value" not in c:
            continue
        item = {"name": c["name"], "value": c["value"], "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)), "httpOnly": bool(c.get("httpOnly", False))}
        if c.get("domain"):
            item["domain"] = c["domain"]
        elif c.get("url"):
            item["url"] = c["url"]
        else:
            continue
        exp = c.get("expires", c.get("expirationDate"))
        if isinstance(exp, (int, float)) and exp > 0 and not c.get("session"):
            item["expires"] = exp
        ss = SAMESITE.get(str(c.get("sameSite", "")).lower())
        if ss:
            item["sameSite"] = ss
        out.append(item)
    return out


def load_cookie_file(path):
    try:
        from cookie_loader import load_cookies
        return load_cookies(path)
    except ImportError:
        return _fallback_load_cookies(path)


def expand_cookie_args(items):
    out = []
    for it in items or []:
        p = Path(it)
        out += [str(f) for f in sorted(p.glob("*.json"))] if p.is_dir() else [str(p)]
    return out


# ---------------------------------------------------------------- page helpers
def dismiss_popups(page):
    for name in POPUP_NAMES:
        try:
            b = page.get_by_role("button", name=name, exact=True).first
            if b.is_visible():
                b.click(timeout=1500)
                log(f"closed popup: {name}")
                page.wait_for_timeout(400)
        except Exception:
            pass


def click_first(page, label, candidates, timeout=3000):
    """Click the first visible candidate locator. True on success."""
    for desc, loc in candidates:
        try:
            el = loc.first
            if el.is_visible():
                el.click(timeout=timeout)
                log(f"{label}: clicked ({desc})")
                return True
        except Exception as e:
            vlog(f"{label}: {desc} failed: {str(e).splitlines()[0]}")
    return False


def text_candidates(page, pattern):
    rx = re.compile(pattern, re.I)
    return [
        ("button", page.get_by_role("button", name=rx)),
        ("menuitem", page.get_by_role("menuitem", name=rx)),
        ("link", page.get_by_role("link", name=rx)),
        ("tab", page.get_by_role("tab", name=rx)),
        ("text", page.get_by_text(rx)),
        ("aria-label", page.locator(f'[aria-label*="{pattern.split("|")[0].strip("^$()")}" i]')),
    ]


def find_prompt_box(page, strict=False):
    """The 'Describe your video' box. strict=True only accepts a box whose label/placeholder matches."""
    sels = ['textarea:visible', 'div[role="textbox"]:visible', '[contenteditable="true"]:visible',
            'input[type="text"]:visible']
    best = None
    for sel in sels:
        loc = page.locator(sel)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                hint = ((el.get_attribute("placeholder") or "") + " " + (el.get_attribute("aria-label") or ""))
                if re.search(r"describe|prompt|video|create|what", hint, re.I):
                    return el
                best = best or el
            except Exception:
                continue
    return None if strict else best


def type_into(page, el, text):
    """Focus the box even when an overlay span intercepts the click, then type."""
    focused = False
    for how in ("click", "force", "js"):
        try:
            if how == "click":
                el.click(timeout=2500)
            elif how == "force":
                el.click(timeout=2500, force=True)
            else:
                el.evaluate("e => { e.scrollIntoView({block: 'center'}); e.focus(); }")
            focused = True
            vlog(f"prompt box focused via {how}")
            break
        except Exception as e:
            vlog(f"focus via {how} failed: {str(e).splitlines()[0]}")
    if not focused:
        raise RuntimeError("could not focus the prompt box")
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(text)
    page.wait_for_timeout(400)
    try:
        typed = (el.inner_text(timeout=2000) or "").strip()
    except Exception:
        typed = text
    if text[:15] not in typed:
        log("typing did not show up in the box; filling it directly", "WARN")
        el.fill(text)


def attach_image(page, path):
    sp = str(Path(path).resolve())
    cands = text_candidates(page, r"upload|add (an )?image|reference image|from (your )?(computer|device)|image")
    for desc, loc in cands:
        try:
            el = loc.first
            if not el.is_visible():
                continue
            with page.expect_file_chooser(timeout=4000) as fc:
                el.click(timeout=2000)
            fc.value.set_files(sp)
            log(f"image attached ({desc}): {Path(sp).name}")
            return True
        except Exception:
            continue
    try:
        inp = page.locator('input[type="file"]')
        if inp.count():
            inp.first.set_input_files(sp)
            log(f"image attached via file input: {Path(sp).name}")
            return True
    except Exception:
        pass
    return False


def save_video_url(ctx, page, src, path):
    log(f"downloading: {src[:140]}")
    if src.startswith("blob:"):
        path.write_bytes(base64.b64decode(page.evaluate(BLOB_JS, src)))
        return
    errors = []
    for attempt in range(1, 4):
        try:
            r = ctx.request.get(src, timeout=DL_TIMEOUT_MS)
            if not r.ok:
                raise RuntimeError(f"status {r.status}")
            path.write_bytes(r.body())
            return
        except Exception as e:
            errors.append(str(e).splitlines()[0])
            log(f"download attempt {attempt}/3 failed: {errors[-1]}", "WARN")
            page.wait_for_timeout(3000)
    raise RuntimeError("download failed: " + " | ".join(errors))


def download_any(ctx, page, candidates, path):
    """Try each URL. True when a file was saved."""
    for src in candidates:
        try:
            save_video_url(ctx, page, src, path)
            if path.exists() and path.stat().st_size > 50_000:
                log(f"saved {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
                return True
        except Exception as e:
            log(f"URL download failed: {str(e).splitlines()[0]}", "WARN")
    return False


def download_via_button(page, path):
    """Click a visible Download button / menu entry and save the browser download."""
    for desc, loc in text_candidates(page, r"download"):
        try:
            el = loc.first
            if not el.is_visible():
                continue
            with page.expect_download(timeout=DL_TIMEOUT_MS) as dl:
                el.click(timeout=3000)
            dl.value.save_as(str(path))
            log(f"downloaded via {desc} Download: {path.name}")
            return True
        except Exception as e:
            vlog(f"download via {desc} failed: {str(e).splitlines()[0]}")
    return False


def download_via_file_menu(page, path):
    """File > Download > MP4 in the Vids editor."""
    try:
        if not click_first(page, "File menu", text_candidates(page, r"^file$")):
            return False
        page.wait_for_timeout(800)
        click_first(page, "Download submenu", text_candidates(page, r"^download"))
        page.wait_for_timeout(800)
        mp4 = page.get_by_text(re.compile(r"mp4", re.I)).first
        with page.expect_download(timeout=DL_TIMEOUT_MS) as dl:
            mp4.click(timeout=4000)
        dl.value.save_as(str(path))
        log(f"downloaded via File > Download: {path.name}")
        return True
    except Exception as e:
        log(f"File > Download failed: {str(e).splitlines()[0]}", "WARN")
        return False


def get_ratio(args):
    """--ratio, else 'Aspect ratio 9:16' written in the prompt, else landscape."""
    if args.ratio:
        return args.ratio
    m = re.search(r"aspect ratio\s+(\d+:\d+)", args.prompt or "", re.I)
    return m.group(1) if m and m.group(1) in RATIO_LABEL else "16:9"


def tile_candidates(page):
    """The '+' tile under 'Start a new video' on the Vids home page (it has no text)."""
    return [
        ("blank tile image", page.locator("img[src*='blank']")),
        ("template showcase", page.locator(".docs-homescreen-templates-templateview-showcase")),
        ("first template option", page.locator("[role='option']")),
    ]


def wait_prompt_box(page, secs):
    """Poll for the 'Describe your video' box; True as soon as it shows."""
    end = time.time() + secs
    while time.time() < end:
        if find_prompt_box(page, strict=True) is not None:
            return True
        page.wait_for_timeout(400)
    return False


def pick_start_options(page, ratio):
    """'Hello... Let's start creating' dialog: choose Landscape / Portrait / Square, then 'Create AI videos'."""
    page.wait_for_timeout(300)
    label = RATIO_LABEL.get(ratio, "landscape")
    ok = click_first(page, f"aspect ratio ({label})", [
        ("aria-label", page.locator(f'button[aria-label="Create a {label} video"]')),
        ("button text", page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))),
    ])
    if not ok:
        log(f"aspect ratio button '{label}' not found; keeping the default", "WARN")
    page.wait_for_timeout(500)
    if not click_first(page, "Create AI videos", text_candidates(page, r"create ai videos")):
        log("'Create AI videos' card not found in the start dialog", "WARN")
    if not wait_prompt_box(page, 6):
        click_first(page, "Get started", text_candidates(page, r"^get started$"))
        wait_prompt_box(page, 6)


def page_text(page, limit=3000):
    try:
        return " ".join(page.inner_text("body", timeout=3000).split())[:limit]
    except Exception:
        return ""


def open_new_video(page):
    """Click 'Blank video' on the Vids home. Retries, waits for the page, falls back to the create URL.
    True once the editor address (/videos/d/...) is open."""
    for attempt in range(1, 3):
        dismiss_popups(page)
        if click_first(page, "new video", text_candidates(page, NEW_VIDEO_RE) + tile_candidates(page)):
            try:
                page.wait_for_url(re.compile(r"/videos/d/"), timeout=60000)
                return True
            except PWTimeout:
                log("clicked, but the editor did not open; retrying", "WARN")
        else:
            log(f"'Blank video' not found (attempt {attempt}/2); the page says: {page_text(page, 300)}", "WARN")
        page.wait_for_timeout(2000)
    log("trying the direct create address as a fallback", "WARN")
    try:
        page.goto(CREATE_URL, wait_until="domcontentloaded")
        page.wait_for_url(re.compile(r"/videos/d/"), timeout=45000)
        return True
    except Exception as e:
        log(f"direct create address failed: {str(e).splitlines()[0]}", "WARN")
        return False


# ---------------------------------------------------------------- one account
def run_account(browser, cookie_file, args):
    """Returns (result, None, submitted) or (None, error, submitted)."""
    tag = Path(cookie_file).stem
    outdir = Path(args.outdir)
    state = {"submitted": False}
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    page = None
    try:
        if not Path(cookie_file).exists():
            return None, "cookie file not found", False
        cookies = load_cookie_file(cookie_file)
        try:
            ctx.add_cookies(cookies)
        except Exception:
            bad = 0
            for c in cookies:                      # skip any single cookie Playwright rejects
                try:
                    ctx.add_cookies([c])
                except Exception:
                    bad += 1
            log(f"{bad} cookie(s) rejected", "WARN")
        log(f"loaded {len(cookies)} cookies from {cookie_file}")

        page = ctx.new_page()
        net_videos = []

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                if ct.startswith("video/") and resp.url not in net_videos:
                    net_videos.append(resp.url)
                    log(f"network video: {resp.url[:140]}")
            except Exception:
                pass

        page.on("response", on_response)
        page.on("pageerror", lambda e: log(f"page error: {str(e)[:160]}", "DEBUG"))

        def srcs():
            out = []
            for fr in page.frames:
                try:
                    out += fr.evaluate(ALL_SRC_JS)
                except Exception:
                    pass
            return list(dict.fromkeys(out + net_videos))

        def debug(reason):
            quiet_snap(page, f"debug_vids_{tag}.png")
            try:
                Path(f"debug_vids_controls_{tag}.txt").write_text(page.evaluate(CONTROLS_JS), encoding="utf-8")
            except Exception:
                pass
            try:
                Path(f"debug_vids_text_{tag}.txt").write_text(page_text(page, 20000), encoding="utf-8")
            except Exception:
                pass
            log(f"debug saved: debug_vids_{tag}.png, debug_vids_controls_{tag}.txt, "
                f"debug_vids_text_{tag}.txt ({reason}); page url: {page.url}", "WARN")

        # 1) Vids home
        log(f"opening {HOME_URL}")
        page.goto(HOME_URL, wait_until="domcontentloaded")
        try:
            page.get_by_text("Start a new video").first.wait_for(state="visible", timeout=25000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
        dismiss_popups(page)
        try:
            title = page.title()
        except Exception:
            title = ""
        log(f"at {page.url} (title: {title})")
        if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
            debug("not signed in")
            return None, f"cookies expired or invalid for '{tag}'; export a fresh cookie file", False
        quiet_snap(page, outdir / "live.png")

        # 2) new blank video
        if not open_new_video(page):
            debug("new video button")
            return None, ("could not find or open the 'Blank video' button (see debug_vids_text_"
                          f"{tag}.txt: the account may not have Google Vids / video generation)"), False
        try:
            page.locator('button[aria-label^="Create a "][aria-label$=" video"]').first.wait_for(
                state="visible", timeout=20000)
        except PWTimeout:
            log("the start dialog did not appear; continuing", "WARN")
        log(f"editor open: {page.url}")
        quiet_snap(page, outdir / "live.png")

        # 3) start dialog: aspect ratio + "Create AI videos", then the prompt box
        ratio = get_ratio(args)
        log(f"aspect ratio: {ratio} ({RATIO_LABEL[ratio]})")
        pick_start_options(page, ratio)
        box = find_prompt_box(page, strict=True)
        if box is not None:
            log("the 'Describe your video' box is open")
        else:
            opened = click_first(page, "generate video",
                                 text_candidates(page, r"generate (a )?video|generate video clips|create (a )?video with"))
            if not opened:
                log("trying the menu search (Alt+/)")
                page.keyboard.press("Alt+/")
                page.wait_for_timeout(800)
                page.keyboard.insert_text("Generate a video")
                page.wait_for_timeout(1200)
                page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            dismiss_popups(page)
            box = find_prompt_box(page)
        quiet_snap(page, outdir / "live.png")
        if box is None:
            debug("prompt box")
            return None, "could not find the video prompt box", False

        # 3b) optional reference image (Vids takes one)
        images = [str(Path(p).resolve()) for p in (args.image or [])]
        for p in images:
            if not Path(p).exists():
                return None, f"image not found: {p}", False
            if Path(p).stat().st_size > MAX_IMAGE_MB * 1024 * 1024:
                return None, f"image over {MAX_IMAGE_MB} MB: {Path(p).name}", False
        if images:
            if len(images) > 1:
                log("Vids image-to-video takes one image; the others are ignored", "WARN")
            if not attach_image(page, images[0]):
                debug("image upload")
                return None, "could not attach the reference image", False
            page.wait_for_timeout(3000)
            box = find_prompt_box(page, strict=True) or box

        # 4) prompt
        type_into(page, box, args.prompt)
        log(f"prompt typed ({len(args.prompt)} chars)")
        page.wait_for_timeout(800)
        before = set(srcs())

        # 5) generate
        state["submitted"] = True
        if not click_first(page, "submit", text_candidates(page, r"^(generate|create|submit|send)( video)?$")):
            log("no Generate button found; pressing Enter", "WARN")
            page.keyboard.press("Enter")
        log("Generate pressed; waiting for the clip...")
        page.wait_for_timeout(1500)
        quiet_snap(page, outdir / "live.png")

        # 6) wait
        start = time.time()
        timeout_s = args.timeout_min * 60
        found, ready_at, idle_since, last_shot, last_print = [], None, None, start, 0
        while time.time() - start < timeout_s:
            for s in srcs():
                if s not in before and s not in found:
                    found.append(s)
                    log(f"new video URL: {s[:140]}")
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""

            if time.time() - last_print > 30:
                log(f"waiting {fmt(time.time() - start)}: new videos={len(found)}, busy={bool(BUSY_RE.search(body))}")
                last_print = time.time()
            if time.time() - last_shot > 30:
                quiet_snap(page, outdir / "live.png")
                last_shot = time.time()

            if not found:
                if LIMIT_RE.search(body):
                    log("Vids says the generation limit is reached on this account")
                    debug("limit")
                    return None, "usage limit reached on this account", False
                try:
                    failed_panel = page.get_by_text(re.compile(r"^try again$", re.I)).first.is_visible()
                except Exception:
                    failed_panel = False
                if failed_panel or ERROR_RE.search(body):
                    debug("generation error")
                    return None, "Vids could not generate the video (see debug screenshot)", True

            try:
                if page.get_by_role("button", name=READY_RE).first.is_visible():
                    ready_at = ready_at or time.time()
            except Exception:
                pass

            busy = bool(BUSY_RE.search(body))
            idle_since = None if busy else (idle_since or time.time())
            t = time.time()
            if (found or ready_at) and ((ready_at and t - ready_at > 5) or (idle_since and t - idle_since > IDLE_DONE_S)):
                log("clip is ready")
                break
            page.wait_for_timeout(1000)
        else:
            log(f"gave up waiting after {fmt(timeout_s)}", "WARN")

        try:
            page.screenshot(path=str(outdir / "final.png"), full_page=True)
            log(f"screenshot saved: {outdir / 'final.png'}")
        except Exception:
            quiet_snap(page, outdir / "final.png")

        # 7) download (video URLs first, then the Download button, then File > Download)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out = outdir / f"vids_{stamp}.mp4"
        cands = [s for s in found if not s.startswith("blob:")] + [s for s in found if s.startswith("blob:")]
        saved = download_any(ctx, page, cands, out)
        if not saved:
            saved = download_via_button(page, out)
        if not saved:
            click_first(page, "insert clip", text_candidates(page, r"^(insert|add to (video|scene)|use (this )?video)$"))
            page.wait_for_timeout(4000)
            saved = download_via_file_menu(page, out)
        if not saved:
            debug("download")
            return None, "video was generated but could not be downloaded (open the Vids file from your Drive)", True

        files = [str(out)]

        return {"ok": True, "prompt": args.prompt, "cookie_file": cookie_file, "credits_before": None,
                "credits_after": None, "files": files, "screenshot": str(outdir / "final.png"),
                "vids_url": page.url}, None, True

    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        if page is not None:
            quiet_snap(page, f"debug_vids_{tag}.png")
        return None, f"timeout: {str(e).splitlines()[0]}", state["submitted"]
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}", state["submitted"]
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main():
    global LOG_FILE, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="")
    ap.add_argument("--outdir", default="outputs_vids")
    ap.add_argument("--cookies", nargs="*", default=[],
                    help="cookie JSON files (or a folder of them) of your Google account")
    ap.add_argument("--image", nargs="+", default=None)
    ap.add_argument("--ratio", default=None, choices=list(RATIO_LABEL),
                    help="video shape; default: read from the prompt ('Aspect ratio 9:16'), else 16:9")
    ap.add_argument("--timeout-min", type=int, default=20)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--log-file", default=None)
    # accepted so the shared Streamlit page can pass its usual flags; not used here
    ap.add_argument("--min-credits", type=int, default=0)
    ap.add_argument("--allow-unknown-credits", action="store_true")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-webm", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    if not args.prompt.strip():
        fail("--prompt is required")
    accounts = expand_cookie_args(args.cookies)
    if not accounts:
        fail("Google Vids needs a signed-in account: pass at least one cookie file with --cookies")

    t0 = time.time()
    log("==== run started (Google Vids) ====")
    log(f"prompt: {args.prompt}")
    log(f"images={len(args.image) if args.image else 0} accounts={len(accounts)} ratio={get_ratio(args)}")

    errors = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        try:
            for n, cf in enumerate(accounts, 1):
                log(f"=== account {n}/{len(accounts)}: {cf} ===")
                result, err, submitted = run_account(browser, cf, args)
                if result:
                    Path("result_vids.json").write_text(json.dumps(result, indent=2))
                    log(f"SUCCESS; total time {fmt(time.time() - t0)}")
                    print(json.dumps(result), flush=True)
                    return
                errors[cf] = err
                if submitted:
                    fail({"message": f"stopped: the request was already submitted on {cf}, so no other "
                                     f"account was tried. Check that account's Vids files in Drive.",
                          "details": errors})
                log(f"{cf}: {err} -> trying next", "WARN")
        finally:
            try:
                browser.close()
            except Exception:
                pass
    fail({"message": "all accounts failed", "details": errors})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        log("fatal error:\n" + traceback.format_exc(), "ERROR")
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)