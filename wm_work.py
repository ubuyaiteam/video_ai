"""Worker for geminiwatermarkremover.io/video (Gemini / Veo video watermark remover).

The site cleans the video locally in the browser, so there is no login and no cookies.
Flow per video:  Choose video -> file chooser -> "Start local cleanup" -> wait -> Download.

Usage:
    python wm_work.py --input clip1.mp4 clip2.mp4 --outdir outputs_wm [--headed] [--verbose]

Prints one JSON line at the end: {"ok": true, "files": [...], "failed": {...}}
"""
import argparse
import base64
import json
import platform
import re
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SITE_URL = "https://geminiwatermarkremover.io/video"
VIDEO_EXTS = (".mp4", ".webm", ".mov")
SOFT_LIMIT_MB = 100                     # the site says files under 100 MB are the safest

CHOOSE_RE = re.compile(r"choose video", re.I)
START_RE = re.compile(r"start local cleanup", re.I)
DOWNLOAD_RE = re.compile(r"download", re.I)
PERCENT_RE = re.compile(r"\b(\d{1,3})\s?%")
# Only strong failure phrases, so the FAQ text on the page never triggers a false alarm.
FAIL_RE = re.compile(r"cleanup failed|processing failed|export failed|unsupported (video|format)|"
                     r"could not (process|decode|read)|something went wrong", re.I)

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
        vlog(f"screenshot saved: {path}")
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


# ---------------------------------------------------------------- page helpers
def body_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def attach_video(page, path):
    """Click 'Choose video' and answer the native file chooser. Falls back to the file input."""
    sp = str(Path(path).resolve())
    try:
        btn = page.get_by_role("button", name=CHOOSE_RE).first
        btn.wait_for(state="visible", timeout=20000)
        with page.expect_file_chooser(timeout=8000) as fc:
            btn.click()
        fc.value.set_files(sp)
        log("video attached via file chooser")
        return True
    except Exception as e:
        log(f"file chooser route failed ({str(e).splitlines()[0]}); trying the file input", "WARN")
    try:
        inp = page.locator('input[type="file"]')
        log(f"file inputs on page: {inp.count()}")
        if inp.count():
            inp.first.set_input_files(sp)
            log("video attached via file input")
            return True
    except Exception as e:
        log(f"file input route failed: {e}", "WARN")
    return False


def enabled_downloads(page):
    """Visible, enabled Download buttons/links right now."""
    out = []
    for loc in (page.get_by_role("button", name=DOWNLOAD_RE),
                page.get_by_role("link", name=DOWNLOAD_RE)):
        try:
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                if el.is_visible() and el.is_enabled():
                    out.append(el)
        except Exception:
            pass
    return out


def save_download(page, ctx, el, dest_base):
    """Click a download element and save the file. Returns the saved Path or None."""
    try:
        with page.expect_download(timeout=60000) as dl:
            el.click(timeout=5000)
        d = dl.value
        ext = Path(d.suggested_filename or "").suffix.lower()
        if ext not in VIDEO_EXTS:
            ext = ".mp4"
        path = dest_base.with_suffix(ext)
        d.save_as(str(path))
        log(f"download saved: {path.name} ({path.stat().st_size} bytes) from '{d.suggested_filename}'")
        return path
    except Exception as e:
        log(f"browser download failed: {str(e).splitlines()[0]}", "WARN")

    # fallback: the link points at a blob/http URL
    try:
        href = el.get_attribute("href")
        if href:
            if href.startswith("blob:"):
                data = base64.b64decode(page.evaluate(BLOB_JS, href))
            else:
                r = ctx.request.get(href)
                if not r.ok:
                    raise RuntimeError(f"status {r.status}")
                data = r.body()
            path = dest_base.with_suffix(".mp4")
            path.write_bytes(data)
            log(f"saved via href: {path.name} ({len(data)} bytes)")
            return path
    except Exception as e:
        log(f"href download failed: {e}", "WARN")
    return None


# ---------------------------------------------------------------- one video
def clean_one(browser, src, args, stamp, idx, total):
    """Returns (saved_path, None) or (None, error_text)."""
    src = Path(src)
    outdir = Path(args.outdir)
    tag = f"{idx}of{total}"
    if not src.is_file():
        return None, f"file not found: {src}"
    if src.suffix.lower() not in VIDEO_EXTS:
        return None, f"not an mp4/webm/mov file: {src.name}"
    mb = src.stat().st_size / 1024 / 1024
    log(f"--- video {idx}/{total}: {src.name} ({mb:.1f} MB) ---")
    if mb > SOFT_LIMIT_MB:
        log(f"file is over {SOFT_LIMIT_MB} MB; the site may struggle (its CLI is better for big files)", "WARN")

    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    page = None
    try:
        page = ctx.new_page()
        page.on("pageerror", lambda e: log(f"page error: {str(e)[:300]}", "WARN"))
        page.on("console", lambda m: vlog(f"console.{m.type}: {m.text[:200]}"))
        page.on("requestfailed", lambda r: vlog(f"request failed: {r.url[:140]}"))

        log(f"opening {SITE_URL}")
        page.goto(SITE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for name in ("Not now", "Close", "Got it", "Accept", "Skip"):   # cookie/consent popups
            try:
                b = page.get_by_role("button", name=name, exact=True).first
                if b.is_visible():
                    b.click(timeout=1500)
                    log(f"closed popup: {name}")
            except Exception:
                pass

        # 1) attach the video
        if not attach_video(page, src):
            snap(page, outdir / f"debug_{tag}.png")
            return None, "could not attach the video (Choose video button / file input not found)"

        # 2) wait for the Start button (the site loads the preview first)
        start = page.get_by_role("button", name=START_RE).first
        try:
            start.wait_for(state="visible", timeout=120000)
        except PWTimeout:
            snap(page, outdir / f"debug_{tag}.png")
            return None, "'Start local cleanup' button never appeared"
        for _ in range(60):
            if start.is_enabled():
                break
            page.wait_for_timeout(1000)
        log("video loaded; 'Start local cleanup' is ready")
        snap(page, outdir / "live.png")

        baseline = len(enabled_downloads(page))
        vlog(f"enabled download controls before start: {baseline}")

        # 3) start the cleanup
        start.click(timeout=10000)
        log("clicked Start local cleanup; waiting for it to finish...")
        t0 = time.time()
        timeout_s = args.timeout_min * 60
        last_print, last_shot, last_pct = 0, 0, None
        done_el = None
        while time.time() - t0 < timeout_s:
            body = body_text(page)
            m = FAIL_RE.search(body)
            if m:
                snap(page, outdir / f"debug_{tag}.png")
                return None, f"the site reported a problem: '{m.group(0)}'"

            pcts = PERCENT_RE.findall(body)
            pct = pcts[-1] if pcts else None
            if pct != last_pct and pct is not None:
                log(f"progress: {pct}%")
                last_pct = pct

            found = enabled_downloads(page)
            if len(found) > baseline:
                done_el = found[-1]
                log(f"download button appeared after {fmt_elapsed(time.time() - t0)}")
                break

            if time.time() - last_print > 20:
                tail = body[-300:].replace("\n", " ")
                log(f"waiting {fmt_elapsed(time.time() - t0)} ... page: ...{tail}")
                last_print = time.time()
            if time.time() - last_shot > 15:
                snap(page, outdir / "live.png")
                last_shot = time.time()
            page.wait_for_timeout(2000)

        snap(page, outdir / "final.png")
        if done_el is None:
            snap(page, outdir / f"debug_{tag}.png")
            try:
                (outdir / f"debug_page_{tag}.html").write_text(page.content(), encoding="utf-8")
                log(f"page HTML saved: debug_page_{tag}.html")
            except Exception:
                pass
            return None, f"cleanup did not finish within {args.timeout_min} min (no Download button appeared)"

        # 4) download
        dest_base = outdir / f"{src.stem}_clean_{stamp}"
        saved = save_download(page, ctx, done_el, dest_base)
        if not saved:
            snap(page, outdir / f"debug_{tag}.png")
            return None, "the Download button was found but the file could not be saved"
        return saved, None

    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        if page is not None:
            snap(page, outdir / f"debug_{tag}.png")
        return None, f"timeout: {str(e).splitlines()[0]}"
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}"
    finally:
        ctx.close()


# ---------------------------------------------------------------- main
def main():
    global LOG_FILE, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", nargs="+", required=True, help="one or more video files (mp4/webm/mov)")
    ap.add_argument("--outdir", default="outputs_wm")
    ap.add_argument("--timeout-min", type=int, default=20, help="max minutes to wait per video")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    log("==== watermark remover run started ====")
    log(f"python {platform.python_version()} on {platform.platform()}")
    log(f"videos: {len(args.input)}; outdir={args.outdir}; timeout_min={args.timeout_min}; headed={args.headed}")
    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    files, failed = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        log(f"chromium launched (headless={not args.headed})")
        try:
            for i, src in enumerate(args.input, 1):
                saved, err = clean_one(browser, src, args, stamp, i, len(args.input))
                if saved:
                    files.append(str(saved))
                else:
                    failed[str(src)] = err
                    log(f"{src}: {err}", "ERROR")
        finally:
            browser.close()
            log("browser closed")

    if not files:
        fail({"message": "no video was cleaned", "details": failed})
    result = {"ok": True, "files": files, "failed": failed}
    Path("result_wm.json").write_text(json.dumps(result, indent=2))
    log(f"DONE: {len(files)} cleaned, {len(failed)} failed, total time {fmt_elapsed(time.time() - t0)}")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()