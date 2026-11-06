"""Run once next to agent_work.py:   python patch_agent_work.py
Makes a backup (agent_work.py.bak) and then:
  1. downloads with a 10-minute timeout, 3 tries, then two fallback methods
  2. keeps going if one clip fails, saves the URLs, tries the Download button
  3. saves a live screenshot (outputs/live.png, every 30 s) and the final one (outputs/final.png)
  4. adds "screenshot" to the result JSON
"""
import shutil
import sys
from pathlib import Path

P = Path("agent_work.py")
if not P.exists():
    sys.exit("agent_work.py not found in this folder")
shutil.copy(P, "agent_work.py.bak")
s = P.read_text(encoding="utf-8")


def swap(old, new, what):
    global s
    if old not in s:
        sys.exit(f"anchor not found for: {what}  (nothing was changed; file is untouched)")
    s = s.replace(old, new, 1)


# 1) robust save_video ------------------------------------------------------
a = s.index("def save_video(ctx, page, src, path):")
b = s.index("# ---------------------------------------------------------------- finding the video")
NEW_SAVE = '''DL_TIMEOUT_MS = 600_000   # big videos need far more than Playwright's 30 s default


def save_video(ctx, page, src, path):
    log(f"downloading: {src[:140]}")
    if src.startswith("blob:"):
        data = base64.b64decode(page.evaluate(BLOB_JS, src))
        path.write_bytes(data)
        log(f"saved {path} ({len(data)} bytes)")
        return
    errors = []
    # 1) Playwright request with a long timeout, 3 tries
    for attempt in range(1, 4):
        try:
            r = ctx.request.get(src, timeout=DL_TIMEOUT_MS)
            log(f"download HTTP {r.status}, content-type {r.headers.get('content-type')}")
            if not r.ok:
                raise RuntimeError(f"download status {r.status}")
            data = r.body()
            path.write_bytes(data)
            log(f"saved {path} ({len(data)} bytes)")
            return
        except Exception as e:
            msg = str(e).splitlines()[0]
            errors.append(msg)
            log(f"download attempt {attempt}/3 failed: {msg}", "WARN")
            page.wait_for_timeout(3000)
    # 2) plain streaming download straight to disk
    try:
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
        if path.stat().st_size > 0:
            log(f"saved {path} ({path.stat().st_size} bytes) via streaming download")
            return
    except Exception as e:
        errors.append(str(e).splitlines()[0])
        log(f"streaming download failed: {errors[-1]}", "WARN")
    # 3) fetch inside the page
    try:
        data = base64.b64decode(page.evaluate(BLOB_JS, src))
        path.write_bytes(data)
        log(f"saved {path} ({len(data)} bytes) via in-page fetch")
        return
    except Exception as e:
        errors.append(str(e).splitlines()[0])
    raise RuntimeError("all download methods failed: " + " | ".join(errors))


'''
s = s[:a] + NEW_SAVE + s[b:]

# 2) keep going when one clip fails ----------------------------------------
swap("        parts, seen_hash = [], set()\n",
     "        parts, seen_hash, failed_urls = [], set(), []\n", "parts init")
swap("            save_video(ctx, page, src, path)\n",
     "            try:\n"
     "                save_video(ctx, page, src, path)\n"
     "            except Exception as e:\n"
     "                log(f\"could not download video {i}: {e}\", \"ERROR\")\n"
     "                failed_urls.append(src)\n"
     "                path.unlink(missing_ok=True)\n"
     "                continue\n", "save_video call")
swap("        parts += [Path(p) for p in downloaded]\n",
     "        if failed_urls and not parts and not downloaded:\n"
     "            downloaded = try_download_button(page, outdir, stamp)\n"
     "        parts += [Path(p) for p in downloaded]\n"
     "        if failed_urls:\n"
     "            urls_file = outdir / f\"video_urls_{stamp}.txt\"\n"
     "            urls_file.write_text(\"\\n\".join(failed_urls), encoding=\"utf-8\")\n"
     "            log(f\"URLs that could not be downloaded saved to {urls_file}\", \"WARN\")\n"
     "        if not parts:\n"
     "            snap(page, outdir / \"final.png\")\n"
     "            return None, (f\"video was generated but could not be downloaded; open the URLs in \"\n"
     "                          f\"outputs/video_urls_{stamp}.txt in your browser\"), True\n", "parts += downloaded")

# 3) screenshots -----------------------------------------------------------
swap("            if time.time() - last_shot > 120:\n"
     "                snap(page, f\"progress_{tag}.png\")\n"
     "                last_shot = time.time()\n",
     "            if time.time() - last_shot > 30:\n"
     "                try:\n"
     "                    page.screenshot(path=str(outdir / \"live.png\"))\n"
     "                except Exception:\n"
     "                    pass\n"
     "                last_shot = time.time()\n", "progress screenshot")
swap("        log(f\"page url after submit: {page.url}\")\n",
     "        log(f\"page url after submit: {page.url}\")\n"
     "        try:\n"
     "            page.screenshot(path=str(outdir / \"live.png\"))\n"
     "        except Exception:\n"
     "            pass\n", "screenshot after submit")
swap("        snap(page, outdir / \"final.png\")\n",
     "        try:\n"
     "            page.screenshot(path=str(outdir / \"final.png\"), full_page=True)\n"
     "            log(f\"screenshot saved: {outdir / 'final.png'}\")\n"
     "        except Exception:\n"
     "            snap(page, outdir / \"final.png\")\n", "final screenshot")

# 4) result JSON -----------------------------------------------------------
swap("                \"files\": files}, None, True\n",
     "                \"files\": files, \"screenshot\": str(outdir / \"final.png\")}, None, True\n", "result dict")

P.write_text(s, encoding="utf-8")
print("agent_work.py patched (backup: agent_work.py.bak)")