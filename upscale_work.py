"""Upscaler worker: uploads a video to https://free.upscaler.video/ in a real browser, starts
the upscale, waits for it and saves the result.

That site upscales INSIDE the browser with WebGPU (nothing is uploaded to a server), so:
  * it needs Google Chrome or Edge installed (Playwright's bundled Chromium can't play H.264)
  * it needs a GPU; the browser window is shown by default (headless often has no WebGPU)
  * limits of the site: MP4 or WebM, up to 100 MB and 5 minutes per video

If a step fails it saves debug_upscale.png and debug_upscale_controls.txt next to this script.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://free.upscaler.video/"
DL_TIMEOUT_MS = 600_000
MAX_MB = 100

PROGRESS_RE = re.compile(r"upscaling\s*(\d{1,3})\s*%", re.I)
BAD_RE = re.compile(r"cannot play this video|can.t process this file format|HDR video is not supported|"
                    r"An error occurred while processing", re.I)
CONTROLS_JS = """
() => Array.from(document.querySelectorAll('button, [role="button"], select, input, label, a'))
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => e.tagName + ' | type=' + (e.getAttribute('type') || '') + ' | id=' + e.id
     + ' | aria=' + (e.getAttribute('aria-label') || '') + ' | ' + (e.innerText || e.value || '').trim().slice(0, 50).replace(/\\n/g, ' '))
  .join('\\n')
"""

DROP_JS = """
async ([b64, name, mime]) => {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([arr], name, {type: mime}));
  const target = document.querySelector('#file-load-panel') || document.body;
  for (const type of ['dragenter', 'dragover', 'drop'])
    target.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
  return true;
}
"""

# The site's "Choose a video" button uses the File System Access API (showOpenFilePicker) when it
# exists. That opens a native dialog Playwright cannot control, so it is removed before the page
# loads; the site then falls back to a normal <input type=file>, which Playwright can fill.
NO_NATIVE_PICKER_JS = """
for (const n of ['showOpenFilePicker', 'showSaveFilePicker', 'showDirectoryPicker']) {
  try { delete Window.prototype[n]; } catch (e) {}
  try { delete window[n]; } catch (e) {}
}
"""

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
    log(f"FAILED: {m}", "ERROR")
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


def launch(p, headless):
    flags = ["--enable-unsafe-webgpu", "--enable-features=Vulkan", "--ignore-gpu-blocklist", "--enable-gpu"]
    for ch in ("chrome", "msedge"):
        try:
            b = p.chromium.launch(channel=ch, headless=headless, args=flags)
            log(f"browser: installed {ch}")
            return b, ch
        except Exception as e:
            log(f"{ch} not available: {str(e).splitlines()[0]}", "WARN")
    log("using Playwright's bundled Chromium: it cannot decode H.264, so MP4 files are converted to WebM first", "WARN")
    return p.chromium.launch(headless=headless, args=flags), None


def to_webm(src, outdir):
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    dst = outdir / (Path(src).stem + "_for_upscaler.webm")
    log(f"converting to WebM: {dst.name}")
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libvpx-vp9",
                        "-b:v", "0", "-crf", "30", "-an", str(dst)], capture_output=True, text=True)
    return dst if r.returncode == 0 and dst.exists() else None


def pick_option(page, label, value):
    """Select `value` in a dropdown or click a button/label that says `value`. True on success."""
    if not value:
        return True
    rx = re.compile(re.escape(value), re.I)
    exact = re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
    try:                                   # the site's segmented buttons: 1080p / 2K, Sharpen / Detail ...
        seg = page.locator("button.output-seg-btn:visible", has_text=exact)
        if seg.count():
            seg.first.click(timeout=2500)
            log(f"{label}: clicked '{value}'")
            return True
    except Exception:
        pass
    try:                                   # <select> elements
        sels = page.locator("select")
        for i in range(sels.count()):
            opts = sels.nth(i).evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
            for val, text in opts:
                if rx.search(text) or rx.search(val):
                    sels.nth(i).select_option(value=val)
                    log(f"{label}: selected '{text}'")
                    return True
    except Exception:
        pass
    for desc, loc in (("button", page.get_by_role("button", name=exact)),
                      ("radio", page.get_by_role("radio", name=exact)),
                      ("text", page.get_by_text(exact))):
        try:
            el = loc.first
            if el.is_visible():
                el.click(timeout=2500)
                log(f"{label}: clicked '{value}' ({desc})")
                return True
        except Exception:
            continue
    log(f"{label}: could not find '{value}' on the page; leaving the site default", "WARN")
    return False


def run(browser, ch, args):
    outdir = Path(args.outdir)
    src = Path(args.input).resolve()
    work_src = src
    if ch is None and src.suffix.lower() != ".webm":
        conv = to_webm(src, outdir)
        if conv:
            work_src = conv
        else:
            log("ffmpeg not found: the bundled browser may not be able to play this file", "WARN")

    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    ctx.add_init_script(NO_NATIVE_PICKER_JS)
    page = ctx.new_page()
    page.on("pageerror", lambda e: log(f"page error: {str(e)[:200]}", "WARN"))

    def debug(reason):
        quiet_snap(page, "debug_upscale.png")
        try:
            Path("debug_upscale_controls.txt").write_text(page.evaluate(CONTROLS_JS), encoding="utf-8")
            log(f"debug saved: debug_upscale.png, debug_upscale_controls.txt ({reason})", "WARN")
        except Exception:
            pass

    try:
        log(f"opening {URL}")
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        gpu = page.evaluate("""async () => { if (!navigator.gpu) return 'navigator.gpu missing';
            try { const a = await navigator.gpu.requestAdapter(); return a ? 'ok' : 'no GPU adapter'; }
            catch (e) { return 'error: ' + e; } }""")
        log(f"WebGPU check: {gpu}")
        if gpu != "ok":
            quiet_snap(page, outdir / "final.png")
            return None, (f"WebGPU is not available in this browser ({gpu}). Use Chrome/Edge with a GPU and "
                          f"keep 'Show the browser window' ticked.")

        # 1) give it the file: first through the big "Choose a video" button (file chooser),
        #    then through a hidden <input type=file>
        gave = False
        for desc, loc in (("#input-button", page.locator("#input-button")),
                          ("button", page.get_by_role("button", name=re.compile(r"choose a video", re.I))),
                          ("label", page.locator("label", has_text=re.compile(r"choose a video", re.I))),
                          ("text", page.get_by_text(re.compile(r"choose a video", re.I)))):
            try:
                el = loc.first
                if not el.is_visible():
                    continue
                with page.expect_file_chooser(timeout=6000) as fc:
                    el.click(timeout=3000)
                fc.value.set_files(str(work_src))
                gave = True
                log(f"file given through the chooser ({desc}): {work_src.name} ({work_src.stat().st_size / 1e6:.1f} MB)")
                break
            except Exception as e:
                log(f"chooser via {desc} failed: {str(e).splitlines()[0]}", "DEBUG")
        if not gave:
            inputs = page.locator('input[type="file"]')
            try:
                inputs.first.wait_for(state="attached", timeout=4000)
                target = None
                for i in range(inputs.count()):
                    el = inputs.nth(i)
                    accept = (el.get_attribute("accept") or "")
                    if not el.evaluate("e => e.multiple") and ("video" in accept or not accept):
                        target = el
                        break
                (target or inputs.first).set_input_files(str(work_src))
                gave = True
                log(f"file given through the file input: {work_src.name}")
            except Exception as e:
                log(f"file input route failed: {str(e).splitlines()[0]}", "WARN")
        if not gave and work_src.stat().st_size <= 60 * 1024 * 1024:
            try:                              # last resort: drag and drop onto the page
                import base64
                b64 = base64.b64encode(work_src.read_bytes()).decode()
                mime = "video/webm" if work_src.suffix.lower() == ".webm" else "video/mp4"
                page.evaluate(DROP_JS, [b64, work_src.name, mime])
                gave = True
                log("file dropped onto the page (drag and drop)")
            except Exception as e:
                log(f"drag and drop failed: {str(e).splitlines()[0]}", "WARN")
        if not gave:
            debug("could not give the file to the page")
            return None, ("could not hand the file to the page (no 'Choose a video' button or file input "
                          "found); send debug_upscale.png and debug_upscale_controls.txt")

        # 2) wait for the options screen
        start_btn = page.locator("button:visible", has_text=re.compile(r"start upscaling", re.I))
        t0 = time.time()
        while time.time() - t0 < 120:
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            m = BAD_RE.search(body)
            if m:
                debug("site rejected the file")
                return None, f"the site rejected the file: {m.group(0)}"
            try:
                if start_btn.first.is_visible():
                    break
            except Exception:
                pass
            page.wait_for_timeout(1500)
        else:
            debug("options screen")
            return None, "the options screen (Start upscaling) did not appear"
        quiet_snap(page, outdir / "live.png")

        # 3) options (anything not found keeps the site default)
        pick_option(page, "resolution", args.resolution)
        pick_option(page, "model", args.model)
        if args.quality:
            try:
                page.get_by_text(re.compile(r"^\s*Advanced", re.I)).first.click(timeout=2500)
                page.wait_for_timeout(600)
                log("opened the Advanced section")
            except Exception:
                log("Advanced section not found", "WARN")
            pick_option(page, "quality", args.quality)
        try:                                # 'Save to disk?  No / Yes' -> No gives a Download button
            grp = page.get_by_text(re.compile(r"save to disk", re.I)).first.locator("xpath=..")
            grp.get_by_text("No", exact=True).first.click(timeout=2500)
            log("save to disk: No")
        except Exception:
            log("save-to-disk switch not found; continuing", "WARN")
        quiet_snap(page, outdir / "live.png")

        # 4) start
        start_btn.first.click(timeout=5000)
        log("upscaling started")

        # 5) wait for the Download button
        start = time.time()
        timeout_s = args.timeout_min * 60
        last_pct, last_print, last_shot = None, 0, start
        done = False
        dl_btn = page.locator("button:visible, a:visible", has_text=re.compile(r"^\s*download\s*$", re.I))
        while time.time() - start < timeout_s:
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            pm = PROGRESS_RE.search(body)
            pct = int(pm.group(1)) if pm else None
            if pct != last_pct or time.time() - last_print > 30:
                log(f"waiting {fmt(time.time() - start)}: progress {pct if pct is not None else '?'}%")
                last_pct, last_print = pct, time.time()
            if time.time() - last_shot > 20:
                quiet_snap(page, outdir / "live.png")
                last_shot = time.time()
            if BAD_RE.search(body) and pct is None:
                debug("error during upscale")
                return None, "the site reported an error while upscaling"
            try:
                if dl_btn.first.is_visible():
                    done = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(2500)
        quiet_snap(page, outdir / "final.png", full=True)
        if not done:
            debug("timeout")
            return None, f"upscaling did not finish within {args.timeout_min} minutes"
        log(f"upscaling finished after {fmt(time.time() - start)}")

        # 6) download
        with page.expect_download(timeout=DL_TIMEOUT_MS) as dl:
            dl_btn.first.click(timeout=5000)
        d = dl.value
        ext = Path(d.suggested_filename or "").suffix.lower() or ".mp4"
        out = outdir / f"{src.stem}_upscaled_{time.strftime('%H%M%S')}{ext}"
        d.save_as(str(out))
        log(f"saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
        return {"ok": True, "input": str(src), "files": [str(out)],
                "screenshot": str(outdir / "final.png")}, None
    except PWTimeout as e:
        log(f"Playwright timeout: {e}", "ERROR")
        debug("timeout")
        return None, f"timeout: {str(e).splitlines()[0]}"
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        return None, f"{type(e).__name__}: {e}"
    finally:
        ctx.close()


def main():
    global LOG_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", default="outputs_upscaled")
    ap.add_argument("--resolution", default="", help="text of the resolution option on the site, e.g. 1080p")
    ap.add_argument("--model", default="", help="e.g. Sharpen or Detail")
    ap.add_argument("--quality", default="", help="Fast, Balanced or Quality")
    ap.add_argument("--timeout-min", type=int, default=60)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args()
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(args.outdir).mkdir(parents=True, exist_ok=True)

    src = Path(args.input)
    if not src.is_file():
        fail(f"input video not found: {src}")
    if src.suffix.lower() not in (".mp4", ".webm"):
        fail("the site only accepts MP4 or WebM files")
    if src.stat().st_size > MAX_MB * 1024 * 1024:
        fail(f"file is {src.stat().st_size / 1e6:.0f} MB; the site accepts at most {MAX_MB} MB")
    log("==== upscale run started ====")
    log(f"input: {src}  resolution={args.resolution or '-'} model={args.model or '-'} quality={args.quality or '-'}")

    with sync_playwright() as p:
        browser, ch = launch(p, args.headless)
        try:
            result, err = run(browser, ch, args)
        finally:
            browser.close()
    if result:
        Path("result_upscale.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    else:
        fail(err)


if __name__ == "__main__":
    main()