"""There's An AI For That (TAAFT) free video generator worker.

Opens https://theresanaiforthat.com/@taaft/video-generator/, fills the prompt, attaches up to 3
reference images, picks model (Fast / Quality), orientation and "Enhance my prompt", presses
Generate, waits for the clip (about 6-11 minutes) and downloads it (8 s clips).

Cookies are optional: without them the site runs as a visitor, with them it runs as your TAAFT
account (cookie files of theresanaiforthat.com, tried in order; an account with 0 videos left
today is skipped). Needs cookie_loader.py only when cookie files are used.

If a step fails it saves debug_taaft_<account>.png and debug_taaft_controls_<account>.txt.

First-time setup:
    pip install playwright
    playwright install chromium
"""
import argparse
import base64
import json
import random
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://theresanaiforthat.com/@taaft/video-generator/"
DL_TIMEOUT_MS = 600_000
MAX_IMAGES = 6
MAX_IMAGE_MB = 16

QUOTA_RE = re.compile(r"(\d+)\s+of\s+(\d+)\s+videos?\s+left", re.I)
LOGIN_RE = re.compile(r"(sign|log) ?in to (generate|continue|use)|create (a free )?account to|please (sign|log) ?in", re.I)
ERROR_RE = re.compile(r"something went wrong|generation failed|failed to generate|couldn.t generate|"
                      r"try again later|error occurred|blocked|violat", re.I)
LIMIT_RE = re.compile(r"0 of \d+ videos left|no videos left|daily limit", re.I)

# --- browser launch / anti-detection -------------------------------------------------------
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

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
() => Array.from(document.querySelectorAll('button, [role="radio"], input, textarea, label, a, select'))
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => e.tagName + ' | id=' + e.id + ' | type=' + (e.getAttribute('type') || '') + ' | name=' + (e.getAttribute('name') || '')
     + ' | ' + (e.innerText || e.value || '').trim().slice(0, 50).replace(/\\n/g, ' '))
  .join('\\n')
"""

DEFAULT_EXTEND = "Continue the scene naturally from the last frame."

LOG_FILE = None


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


def fail(m):
    log(f"FAILED: {json.dumps(m) if not isinstance(m, str) else m}", "ERROR")
    print(json.dumps({"ok": False, "error": m}), flush=True)
    sys.exit(1)


def launch_browser(p, headed, slow_mo=0):
    """Try installed Chrome, then Edge, then bundled Chromium."""
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--disable-dev-shm-usage"]
    last = None
    for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            b = p.chromium.launch(headless=not headed, args=args, slow_mo=slow_mo,
                                  ignore_default_args=["--enable-automation"], **kw)
            log(f"browser launched: {kw.get('channel', 'bundled chromium')}")
            return b
        except Exception as e:
            last = str(e).splitlines()[0]
            log(f"launch {kw or 'chromium'} failed: {last}", "WARN")
    fail(f"could not launch any browser ({last}). Run: playwright install chromium")


def launch_profile(p, args):
    """Real Chrome with a saved profile folder: logins, cookies and history persist between runs."""
    prof = Path(args.profile_dir).resolve()
    prof.mkdir(parents=True, exist_ok=True)
    last = None
    for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            c = p.chromium.launch_persistent_context(
                str(prof), headless=False, slow_mo=args.slow_mo, accept_downloads=True, locale="en-US",
                viewport={"width": 1400, "height": 1000},
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"], **kw)
            log(f"profile browser launched: {kw.get('channel', 'bundled chromium')} ({prof})")
            return c
        except Exception as e:
            last = str(e).splitlines()[0]
            log(f"profile launch {kw or 'chromium'} failed: {last}", "WARN")
    fail(f"could not launch the profile browser ({last}). Close any Chrome window using this profile.")


def quiet_snap(page, path, full=False):
    try:
        page.screenshot(path=str(path), full_page=full)
    except Exception:
        pass


def fmt(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def jsclick(loc):
    """Click through overlays (the site has a sticky banner) by calling element.click()."""
    loc.first.evaluate("e => { e.scrollIntoView({block: 'center'}); e.click(); }")


def human_pause(page, lo=1500, hi=3500):
    """Random pause so the run does not look scripted."""
    page.wait_for_timeout(random.randint(lo, hi))


def humanclick(page, loc):
    """Real mouse movement + click (trusted events). Falls back to jsclick if it cannot be done."""
    try:
        el = loc.first
        el.evaluate("e => e.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(random.randint(500, 1000))
        bb = el.bounding_box()
        x = bb["x"] + bb["width"] * random.uniform(0.35, 0.65)
        y = bb["y"] + bb["height"] * random.uniform(0.35, 0.65)
        page.mouse.move(x, y, steps=random.randint(15, 30))
        page.wait_for_timeout(random.randint(250, 700))
        page.mouse.click(x, y)
    except Exception as e:
        log(f"mouse click failed ({str(e).splitlines()[0]}); using script click", "DEBUG")
        jsclick(loc)


def read_quota(page):
    try:
        t = page.locator("#videogen_quota").first.inner_text(timeout=3000)
    except Exception:
        return None, ""
    m = QUOTA_RE.search(t)
    return (int(m.group(1)) if m else None), t.strip()


def history_count(page):
    try:
        t = page.locator("#videogen_history").first.inner_text(timeout=2000)
    except Exception:
        return None
    m = re.search(r"(\d+)\s+videos?", t, re.I)
    return int(m.group(1)) if m else None


def dl_link(page):
    """(absolute href, label) of the site's 'Download part N' link, or (None, '')."""
    try:
        loc = page.locator("#videogen_download")
        if loc.count() == 0:
            return None, ""
        a = loc.first
        href = a.get_attribute("href") or ""
        label = " ".join((a.inner_text(timeout=1500) or "").split())
        if not href:
            return None, label
        return page.evaluate("h => new URL(h, location.href).href", href), label
    except Exception:
        return None, ""


def find_extend_box(page):
    """The prompt box of the extend form: prefer one inside an 'extend' element, else any visible
    textarea other than the main prompt, else the main prompt box."""
    for sel in ("#videogen_extend_slot textarea:visible", "#videogen_extend_slot input[type='text']:visible",
                "[id*='extend' i] textarea:visible", "[class*='extend' i] textarea:visible",
                "#videogen_panel textarea:visible", "[id*='extend' i] input[type='text']:visible"):
        loc = page.locator(sel)
        if loc.count():
            return loc.first
    others = page.locator("textarea:visible:not(#user_input)")
    if others.count():
        return others.first
    main = page.locator("#user_input")
    return main.first if main.count() else None


def find_extend_submit(page):
    """The button that starts the extension (not the 'Extend video' opener, not Cancel)."""
    slot_btns = page.locator("#videogen_extend_slot button:visible").filter(
        has_not_text=re.compile(r"cancel", re.I))
    if slot_btns.count():
        pref = slot_btns.filter(has_text=re.compile(r"extend|generate|continue|create|submit|start", re.I))
        return pref.first if pref.count() else slot_btns.last
    for sel in ("button[id*='extend' i]:visible:not(#videogen_extend_open)",
                "button[class*='extend' i]:visible:not(#videogen_extend_open)"):
        loc = page.locator(sel).filter(has_not_text=re.compile(r"cancel", re.I))
        if loc.count():
            return loc.first
    loc = page.locator("button:visible:not(#videogen_extend_open)",
                       has_text=re.compile(r"^\s*(extend|generate|continue|create|submit)( video| part)?\s*$", re.I))
    if loc.count():
        return loc.first
    gen = page.locator("#generate:visible")
    return gen.first if gen.count() else None


def download_any(ctx, page, candidates, path):
    """Try each URL, then the site's own download link. True when a file was saved."""
    for src in candidates:
        try:
            save_video_url(ctx, page, src, path)
            if path.exists() and path.stat().st_size > 50_000:
                log(f"saved {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
                return True
        except Exception as e:
            log(f"URL download failed: {str(e).splitlines()[0]}", "WARN")
    try:
        with page.expect_download(timeout=DL_TIMEOUT_MS) as dl:
            jsclick(page.locator("#videogen_download"))
        dl.value.save_as(str(path))
        ok = path.exists() and path.stat().st_size > 0
        log(f"downloaded via the download link: {path.name}")
        return ok
    except Exception as e:
        log(f"download link failed: {str(e).splitlines()[0]}", "WARN")
        return False


def join_parts(parts, out):
    """Join the parts into one video with ffmpeg. Returns the output path or None."""
    ff = shutil.which("ffmpeg")
    if not ff:
        log("ffmpeg not found: the parts stay separate (winget install Gyan.FFmpeg)", "WARN")
        return None
    import subprocess
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode == 0 and out.exists():
        log(f"joined {len(parts)} parts into {out.name}")
        return out
    log(f"join failed: {r.stderr.strip()[:300]}", "WARN")
    return None


def open_latest_video(page):
    """Open the newest entry of 'Your recent videos' in the player. True when its Extend button shows."""
    for sel in ("#videogen_history video", "#videogen_history [class*='card']", "#videogen_history li",
                "#videogen_history a", "#videogen_history img"):
        try:
            loc = page.locator(sel)
            if not loc.count():
                continue
            jsclick(loc)
            page.locator("#videogen_extend_open").first.wait_for(state="visible", timeout=15000)
            log(f"opened the newest video from the history ({sel})")
            return True
        except Exception:
            continue
    return False


def extension_step(ctx, page, outdir, tag, stamp, n, ptxt, prev, timeout_s, debug):
    """Press 'Extend video', type the prompt, start it, wait and download.
    Returns (path, href, warning); path is None when it failed."""
    log(f"extending the video: part {n} - {ptxt[:100]}")
    try:
        humanclick(page, page.locator("#videogen_extend_open"))
    except Exception as e:
        debug("extend button")
        return None, None, f"part {n}: could not press 'Extend video' ({str(e).splitlines()[0]})"
    page.wait_for_timeout(2500)
    try:
        Path(f"debug_taaft_extend_controls_{tag}.txt").write_text(page.evaluate(CONTROLS_JS), encoding="utf-8")
    except Exception:
        pass
    quiet_snap(page, outdir / "live.png")
    box = find_extend_box(page)
    if box is None:
        debug("extend prompt box")
        return None, None, f"part {n}: the extend form has no prompt box"
    try:
        box.click(timeout=3000, force=True)
        box.fill(ptxt)
    except Exception as e:
        debug("extend prompt")
        return None, None, f"part {n}: could not type the extend prompt ({str(e).splitlines()[0]})"
    submit = find_extend_submit(page)
    if submit is None:
        debug("extend submit")
        return None, None, f"part {n}: the button that starts the extension was not found"
    human_pause(page, 1500, 3000)
    humanclick(page, submit)
    log("extension submitted; waiting for the new part...")
    start, new_href, last_shot, last_print = time.time(), None, time.time(), 0
    while time.time() - start < timeout_s:
        h, lbl = dl_link(page)
        if h and h != prev:
            new_href = h
            log(f"download link appeared: {lbl or h[:100]}")
            break
        if time.time() - last_print > 30:
            log(f"extension waiting {fmt(time.time() - start)}")
            last_print = time.time()
        if time.time() - last_shot > 30:
            quiet_snap(page, outdir / "live.png")
            last_shot = time.time()
        page.wait_for_timeout(3000)
    if not new_href:
        debug("extend wait")
        return None, None, f"part {n}: the extension did not finish within {int(timeout_s // 60)} minutes"
    page.wait_for_timeout(2000)
    path = outdir / f"taaft_{stamp}_part{n}.mp4"
    if not download_any(ctx, page, [new_href], path):
        return None, None, f"part {n} was generated but could not be downloaded"
    return path, new_href, ""


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
    try:
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
        if path.stat().st_size > 0:
            return
    except Exception as e:
        errors.append(str(e).splitlines()[0])
    raise RuntimeError("download failed: " + " | ".join(errors))


def run_account(browser, cookie_file, args, persistent=None):
    """Returns (result, None, submitted) or (None, error, submitted)."""
    tag = Path(cookie_file).stem if cookie_file else "visitor"
    outdir = Path(args.outdir)
    state = {"submitted": False}
    if persistent is not None:
        ctx = persistent
    else:
        ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True,
                                  user_agent=ua, locale="en-US")
    if not args.cdp_url:
        ctx.add_init_script(STEALTH_JS)
    page = None
    try:
        if cookie_file:
            if not Path(cookie_file).exists():
                return None, "cookie file not found", False
            from cookie_loader import load_cookies
            cookies = load_cookies(cookie_file)
            ctx.add_cookies(cookies)
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
            try:
                rq = resp.request
                if (rq.resource_type in ("xhr", "fetch") and "theresanaiforthat.com" in resp.url
                        and (rq.method == "POST" or resp.status >= 400)):
                    body = ""
                    try:
                        body = " ".join(resp.text().split())[:300]
                    except Exception:
                        pass
                    log(f"api {rq.method} {resp.status} {resp.url[:110]} -> {body}",
                        "WARN" if resp.status >= 400 else "DEBUG")
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
            quiet_snap(page, f"debug_taaft_{tag}.png", full=True)
            try:
                Path(f"debug_taaft_controls_{tag}.txt").write_text(page.evaluate(CONTROLS_JS), encoding="utf-8")
            except Exception:
                pass
            try:
                out = page.locator("#generated_output").first.inner_text(timeout=2000)
                Path(f"debug_taaft_output_{tag}.txt").write_text(out, encoding="utf-8")
                log(f"site output: {' '.join(out.split())[:300]}", "WARN")
            except Exception:
                pass
            log(f"debug saved: debug_taaft_{tag}.png, debug_taaft_controls_{tag}.txt ({reason})", "WARN")

        # 1) open the generator
        log(f"opening {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        try:
            page.locator("#user_input").first.wait_for(state="visible", timeout=30000)
        except PWTimeout:
            debug("prompt box not found")
            return None, "the prompt box (#user_input) did not appear", False
        page.wait_for_timeout(2500)
        left, qtext = read_quota(page)
        log(f"quota: {qtext or 'not shown'}")
        if left is not None and left <= 0:
            return None, "no videos left today on this account", False
        quiet_snap(page, outdir / "live.png")

        # 1b) "extend the newest video" mode (used by the Extend button after a finished run)
        if args.extend_last:
            timeout_s = args.timeout_min * 60
            if not open_latest_video(page):
                debug("open newest video")
                return None, ("could not open the newest video from 'Your recent videos' "
                              "(is this the account that made it?)"), False
            prev = dl_link(page)[0]
            stamp = time.strftime("%Y%m%d_%H%M%S")
            ptxt = " ".join(p.strip() for p in (args.extend_prompt or []) if p.strip()) or DEFAULT_EXTEND
            state["submitted"] = True
            partn, new_href, warn = extension_step(ctx, page, outdir, tag, stamp, 2, ptxt, prev, timeout_s, debug)
            if not partn:
                return None, warn or "the extension failed", True
            files = [str(partn)]
            if args.join_with and Path(args.join_with).exists():
                joined = join_parts([Path(args.join_with), partn], outdir / f"taaft_{stamp}_extended.mp4")
                if joined:
                    files = [str(joined), str(partn)]
            try:
                page.screenshot(path=str(outdir / "final.png"), full_page=True)
            except Exception:
                pass
            return {"ok": True, "prompt": ptxt, "cookie_file": cookie_file or "(visitor)", "credits_before": left,
                    "credits_after": None, "quota": read_quota(page)[1], "files": files, "parts": 2, "warning": "",
                    "screenshot": str(outdir / "final.png")}, None, True

        # 2) prompt
        box = page.locator("#user_input").first
        box.click(timeout=5000, force=True)
        box.fill("")
        try:
            box.press_sequentially(args.prompt, delay=args.type_delay)
        except Exception:
            box.fill(args.prompt)
        log(f"prompt typed ({len(args.prompt)} chars)")
        human_pause(page, 2000, 4000)

        # 3) reference images (up to 3)
        images = [str(Path(p).resolve()) for p in (args.image or [])][:MAX_IMAGES]
        if args.image and len(args.image) > MAX_IMAGES:
            log(f"the site takes {MAX_IMAGES} images; using the first {MAX_IMAGES}", "WARN")
        for p in images:
            if not Path(p).exists():
                return None, f"image not found: {p}", False
            if Path(p).stat().st_size > MAX_IMAGE_MB * 1024 * 1024:
                return None, f"image over {MAX_IMAGE_MB} MB: {Path(p).name}", False
        if images:
            done = False
            inp = page.locator("#videogen_images input[type='file']")
            try:
                if inp.count():
                    inp.first.set_input_files(images)
                    done = True
                    log(f"{len(images)} image(s) attached via file input")
            except Exception as e:
                log(f"file input failed: {str(e).splitlines()[0]}", "DEBUG")
            if not done:
                try:
                    with page.expect_file_chooser(timeout=6000) as fc:
                        jsclick(page.locator("#videogen_image_drop"))
                    fc.value.set_files(images)
                    done = True
                    log(f"{len(images)} image(s) attached via file chooser")
                except Exception as e:
                    log(f"file chooser failed: {str(e).splitlines()[0]}", "DEBUG")
            if not done:
                debug("image upload")
                return None, "could not attach the reference image(s)", False
            page.wait_for_timeout(3000)

        # 4) model, orientation, enhance
        try:
            pill = page.locator("#videogen_model_pills button", has_text=re.compile(args.model, re.I))
            jsclick(pill)
            log(f"model: {args.model}")
        except Exception as e:
            log(f"model '{args.model}' not selected ({str(e).splitlines()[0]}); keeping the site default", "WARN")
        try:
            jsclick(page.locator(f"input[name='videogen_orientation'][value='{args.orientation}']"))
            log(f"orientation: {args.orientation}")
        except Exception as e:
            log(f"orientation not set ({str(e).splitlines()[0]})", "WARN")
        try:
            chk = page.locator("#videogen_enhance_prompt").first
            if chk.is_checked() != (not args.no_enhance):
                jsclick(chk)
            log(f"enhance my prompt: {'off' if args.no_enhance else 'on'}")
        except Exception as e:
            log(f"enhance toggle not set ({str(e).splitlines()[0]})", "WARN")
        quiet_snap(page, outdir / "live.png")

        # 5) generate
        before = set(srcs())
        count_before = history_count(page)
        href_before, _ = dl_link(page)
        log(f"videos already on the page: {len(before)}; history count: {count_before}")
        human_pause(page, 3000, 6000)
        state["submitted"] = True
        humanclick(page, page.locator("#generate"))
        log("Generate pressed; waiting for the clip...")
        page.wait_for_timeout(6000)
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if LOGIN_RE.search(body):
            debug("login required")
            return None, "the site asks you to sign in: add cookie files of your TAAFT account", False
        quiet_snap(page, outdir / "live.png")

        # 6) wait
        start = time.time()
        timeout_s = args.timeout_min * 60
        found, found_at, last_shot, last_print, last_out = [], None, start, 0, ""
        found_href, found_href_at = None, None
        attempts = 0
        while time.time() - start < timeout_s:
            for s in srcs():
                if s not in before and s not in found:
                    found.append(s)
                    found_at = found_at or time.time()
                    log(f"new video URL: {s[:140]}")
            h_now, _lbl = dl_link(page)
            if h_now and h_now != href_before and not found_href:
                found_href, found_href_at = h_now, time.time()
                log(f"download link appeared: {_lbl or h_now[:100]}")
            try:
                out_text = " ".join(page.locator("#generated_output").first.inner_text(timeout=2000).split())
            except Exception:
                out_text = ""
            if out_text and out_text != last_out:
                log(f"output: {out_text[:160]}")
                last_out = out_text
            if time.time() - last_print > 30:
                log(f"waiting {fmt(time.time() - start)}: new videos={len(found)}, history={history_count(page)}")
                last_print = time.time()
            if time.time() - last_shot > 30:
                quiet_snap(page, outdir / "live.png")
                last_shot = time.time()
            if not found and not found_href and ERROR_RE.search(out_text):
                if attempts < args.retries:
                    attempts += 1
                    wait_s = 60 * attempts
                    log(f"site error '{out_text[:80]}'; retry {attempts}/{args.retries} in {wait_s}s", "WARN")
                    quiet_snap(page, outdir / f"error_attempt{attempts}.png")
                    page.wait_for_timeout(wait_s * 1000)
                    humanclick(page, page.locator("#generate"))
                    page.wait_for_timeout(8000)
                    last_out = ""
                    start = time.time()
                    continue
                debug("generation error")
                return None, f"the site reported an error: {out_text[:160]}", True
            if found_href and time.time() - found_href_at > 3:
                break
            if found and time.time() - found_at > 8:
                break
            cnt = history_count(page)
            if (not found and cnt is not None and count_before is not None and cnt > count_before
                    and time.time() - start > 60):
                log("history grew; looking for the new video in the list")
                try:
                    jsclick(page.locator("#videogen_history video, #videogen_history [class*='play']"))
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                for s in srcs():
                    if s not in before and s not in found:
                        found.append(s)
                        found_at = found_at or time.time()
                if not found:
                    count_before = cnt        # do not retry every loop
            page.wait_for_timeout(3000)
        else:
            log(f"gave up waiting after {fmt(timeout_s)}", "WARN")

        try:
            page.screenshot(path=str(outdir / "final.png"), full_page=True)
            log(f"screenshot saved: {outdir / 'final.png'}")
        except Exception:
            quiet_snap(page, outdir / "final.png")
        _, qafter = read_quota(page)

        # 7) download part 1 (download link first, then the video URLs, then the button)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        part1 = outdir / f"taaft_{stamp}_part1.mp4"
        cands = ([found_href] if found_href else []) + [s for s in found if not s.startswith("blob:")] \
            + [s for s in found if s.startswith("blob:")]
        if not download_any(ctx, page, cands, part1):
            debug("download")
            return None, "the video was generated but could not be downloaded (see 'Your recent videos' on the site)", True
        parts, warning, current_href = [part1], "", found_href

        # 8) extend the video: one extra 8 s part per extension
        prompts = [p.strip() for p in (args.extend_prompt or []) if p.strip()]
        for i in range(args.extend):
            n = i + 2
            ptxt = prompts[i] if i < len(prompts) else (prompts[-1] if prompts else DEFAULT_EXTEND)
            partn, new_href, warning = extension_step(ctx, page, outdir, tag, stamp, n, ptxt,
                                                      current_href, timeout_s, debug)
            if not partn:
                break
            parts.append(partn)
            current_href = new_href

        # 8b) interactive: keep extending while you type a prompt for what happens next
        if args.extend_interactive and not warning:
            while True:
                print(f"\nPart {len(parts)} is saved. Type what happens next to extend the video "
                      f"(8 s more), or press Enter on an empty line to finish: ",
                      file=sys.stderr, end="", flush=True)
                try:
                    ptxt = sys.stdin.readline().strip()
                except Exception:
                    ptxt = ""
                if not ptxt:
                    break
                n = len(parts) + 1
                partn, new_href, warning = extension_step(ctx, page, outdir, tag, stamp, n, ptxt,
                                                          current_href, timeout_s, debug)
                if not partn:
                    break
                parts.append(partn)
                current_href = new_href

        # 9) final files
        try:
            page.screenshot(path=str(outdir / "final.png"), full_page=True)
        except Exception:
            pass
        if len(parts) == 1:
            single = outdir / f"taaft_{stamp}.mp4"
            parts[0].replace(single)
            files = [str(single)]
        else:
            joined = None if args.no_join else join_parts(parts, outdir / f"taaft_{stamp}_final.mp4")
            files = ([str(joined)] if joined else []) + [str(p) for p in parts]
        if warning:
            log(f"finished with a warning: {warning}", "WARN")

        return {"ok": True, "prompt": args.prompt, "cookie_file": cookie_file or "(visitor)",
                "credits_before": left, "credits_after": None, "quota": qafter, "files": files,
                "parts": len(parts), "warning": warning,
                "screenshot": str(outdir / "final.png")}, None, True

    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        if page is not None:
            quiet_snap(page, f"debug_taaft_{tag}.png")
        return None, f"timeout: {str(e).splitlines()[0]}", state["submitted"]
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}", state["submitted"]
    finally:
        if persistent is None:
            try:
                ctx.close()
            except Exception:
                pass


def expand_cookie_args(items):
    out = []
    for it in items or []:
        p = Path(it)
        out += [str(f) for f in sorted(p.glob("*.json"))] if p.is_dir() else [str(p)]
    return out


def main():
    global LOG_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="")
    ap.add_argument("--outdir", default="outputs_taaft")
    ap.add_argument("--cookies", nargs="*", default=[])
    ap.add_argument("--image", nargs="+", default=None)
    ap.add_argument("--model", default="Fast", choices=["Fast", "Quality"])
    ap.add_argument("--orientation", default="landscape", choices=["landscape", "portrait"])
    ap.add_argument("--no-enhance", action="store_true")
    ap.add_argument("--extend", type=int, default=0, help="how many times to press 'Extend video' (8 s each)")
    ap.add_argument("--extend-prompt", nargs="*", default=[], help="what happens in each extension")
    ap.add_argument("--no-join", action="store_true", help="keep the parts separate (no ffmpeg join)")
    ap.add_argument("--extend-interactive", action="store_true",
                    help="after the video is generated, ask in the terminal what happens next and extend it, "
                         "as many times as you like (press Enter on an empty line to finish)")
    ap.add_argument("--extend-last", action="store_true",
                    help="no new generation: open the newest video in 'Your recent videos' and extend it")
    ap.add_argument("--join-with", default=None, help="with --extend-last: video file to join the new part to")
    ap.add_argument("--timeout-min", type=int, default=30)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--cdp-url", default=None,
                    help="attach to a Chrome you started yourself, e.g. http://localhost:9222 (best against Cloudflare)")
    ap.add_argument("--profile-dir", default=None,
                    help="use real Chrome with this saved profile folder (recommended if the site shows errors)")
    ap.add_argument("--setup-login", action="store_true",
                    help="with --profile-dir: open the site, let you log in / test by hand, then exit")
    ap.add_argument("--slow-mo", type=int, default=300, help="ms delay between every browser action")
    ap.add_argument("--type-delay", type=int, default=70, help="ms delay between typed characters")
    ap.add_argument("--retries", type=int, default=2, help="re-press Generate this many times if the site errors")
    ap.add_argument("--log-file", default=None)
    # accepted so the shared Streamlit page can pass its usual flags; not used here
    ap.add_argument("--min-credits", type=int, default=0)
    ap.add_argument("--allow-unknown-credits", action="store_true")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-webm", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    if not args.extend_last and not args.prompt.strip():
        fail("--prompt is required (or use --extend-last)")
    t0 = time.time()
    log("==== run started (TAAFT video generator) ====")
    log(f"prompt: {args.prompt}")
    log(f"model={args.model} orientation={args.orientation} enhance={'off' if args.no_enhance else 'on'} "
        f"images={len(args.image) if args.image else 0} extend={args.extend}")

    accounts = expand_cookie_args(args.cookies) or [None]
    errors = {}
    with sync_playwright() as p:
        pctx = None
        if args.cdp_url:
            try:
                browser = p.chromium.connect_over_cdp(args.cdp_url, slow_mo=args.slow_mo)
            except Exception as e:
                fail(f"could not connect to Chrome at {args.cdp_url} ({str(e).splitlines()[0]}). "
                     f"Start Chrome with --remote-debugging-port=9222 first.")
            pctx = browser.contexts[0] if browser.contexts else browser.new_context()
            accounts = accounts[:1]
            log(f"attached to your Chrome at {args.cdp_url}")
        elif args.profile_dir:
            pctx = launch_profile(p, args)
            if args.setup_login:
                pg = pctx.pages[0] if pctx.pages else pctx.new_page()
                pg.goto(URL, wait_until="domcontentloaded", timeout=90000)
                print("\nLog in as your TAAFT account in the opened window (you can also try one generation by hand).\n"
                      "When done, come back here and press Enter to save the profile and exit.",
                      file=sys.stderr, flush=True)
                try:
                    sys.stdin.readline()
                except Exception:
                    pass
                pctx.close()
                log("profile saved")
                return
            accounts = accounts[:1]
            browser = None
        else:
            browser = launch_browser(p, args.headed, args.slow_mo)
        try:
            for n, cf in enumerate(accounts, 1):
                log(f"=== account {n}/{len(accounts)}: {cf or 'visitor (no cookies)'} ===")
                result, err, submitted = run_account(browser, cf, args, pctx)
                if result:
                    Path("result_taaft.json").write_text(json.dumps(result, indent=2))
                    log(f"SUCCESS; total time {fmt(time.time() - t0)}")
                    print(json.dumps(result), flush=True)
                    return
                errors[cf or "visitor"] = err
                if submitted:
                    fail({"message": f"stopped: the request was already submitted on {cf or 'the visitor session'}, "
                                     f"so no other account was tried. Check 'Your recent videos' on the site.",
                          "details": errors})
                log(f"{cf}: {err} -> trying next", "WARN")
        finally:
            try:
                (browser or pctx).close()
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