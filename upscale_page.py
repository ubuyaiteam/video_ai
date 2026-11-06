"""Upscaler page: pick a video made by Digen AI or Google Vids and upscale it on free.upscaler.video.

Page of the combined app: run `streamlit run streamlit_app.py`.
Needs upscale_work.py and ui_common.py next to it. Google Chrome (or Edge) must be installed.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

import importlib

import ui_common
importlib.reload(ui_common)          # pick up CSS edits without restarting Streamlit
from ui_common import (card_title, hero, inject_css, parse_result, read_log, show_shot, show_video)

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_upscaled"
LOGDIR = OUTDIR / "logs"
for d in (OUTDIR, LOGDIR):
    d.mkdir(exist_ok=True)
LOG_FILE = LOGDIR / "last_run.log"
LIVE_SHOT = OUTDIR / "live.png"
FINAL_SHOT = OUTDIR / "final.png"
WORKER = BASE / "upscale_work.py" if (BASE / "upscale_work.py").exists() else None
SOURCES = {"Digen AI": BASE / "outputs", "Google Vids": BASE / "outputs_vids"}
MAX_MB = 100
MODELS = ["Site default", "Sharpen", "Detail"]
QUALITIES = ["Fast", "Balanced", "Quality"]
RESOLUTIONS = ["Site default", "1080p", "2K", "4K"]

inject_css()


@st.cache_resource
def job_store():
    return {}


JOBS = job_store()


def browse_video():
    """Button callback: native file dialog (works when the browser and Streamlit run on the same PC)."""
    code = ("import tkinter as tk\nfrom tkinter import filedialog\n"
            "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1)\n"
            "p = filedialog.askopenfilename(title='Select a video', filetypes=["
            "('Videos', '*.mp4 *.webm'), ('All files', '*.*')])\n"
            "root.destroy(); print(p or '')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    if out:
        st.session_state["up_path"] = str(Path(out))
        st.session_state["up_mode"] = "A file path"


def library():
    """(label, path) for every generated video, newest first."""
    rows = []
    for name, folder in SOURCES.items():
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in (".mp4", ".webm") and not p.stem.endswith(("_web", "_for_upscaler")):
                rows.append((p.stat().st_mtime, name, p))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [(f"{n}  |  {time.strftime('%d %b %H:%M', time.localtime(t))}  |  {p.name}  |  "
             f"{p.stat().st_size / 1e6:.1f} MB", p) for t, n, p in rows[:100]]


def start_job(cmd, source):
    now = time.strftime("%H:%M:%S")
    LOG_FILE.write_text(f"{now} [UI] worker: {WORKER}\n{now} [UI] input: {source}\n", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(LOG_FILE, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=fh, stderr=subprocess.STDOUT)
    JOBS["upscale"] = {"proc": proc, "log": LOG_FILE, "started": time.time(), "source": Path(source),
                       "done": False, "result": None}


def stop_job():
    job = JOBS.get("upscale")
    if job and job["proc"].poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(job["proc"].pid), "/T", "/F"], capture_output=True)
        else:
            job["proc"].terminate()


def finalize(job):
    job["done"] = True
    job["result"] = parse_result(read_log(job))
    with open(job["log"], "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} [UI] worker exited with code {job['proc'].returncode}\n")


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Upscale options")
    resolution = st.selectbox("Resolution", RESOLUTIONS,
                              help="The site offers different choices per video (for a 720p clip: 1080p and 2K). "
                                   "If the choice is not available the worker keeps the site's default and says so in the log.")
    model = st.selectbox("AI model", MODELS)
    quality = st.radio("Speed / quality", QUALITIES, index=1, horizontal=True)
    show_browser = st.checkbox("Show the browser window", value=True,
                               help="The site upscales with your GPU (WebGPU). Headless mode usually has no GPU.")
    timeout_min = st.number_input("Give up after (minutes)", min_value=5, value=60, step=5)

# ---------------------------------------------------------------- main
hero("🔍 free.upscaler.video", "Upscale desk",
     "Pick a video from Digen AI or Google Vids and sharpen it to a higher resolution. "
     "The site works inside your own browser, so nothing is uploaded to a server.")

job = JOBS.get("upscale")
running = bool(job) and job["proc"].poll() is None
if job and not running and not job["done"]:
    finalize(job)

left, right = st.columns([1.1, 1], gap="large")
source = None
with left:
    with st.container(border=True):
        card_title("1", "Choose the video", "Videos saved by the Digen AI and Google Vids pages, newest first.")
        mode = st.radio("Source", ["My generated videos", "A file path"], horizontal=True,
                        label_visibility="collapsed", key="up_mode")
        if mode == "My generated videos":
            items = library()
            if items:
                labels = [l for l, _ in items]
                pick = st.selectbox("Video", labels, label_visibility="collapsed")
                source = dict(items)[pick]
            else:
                st.info("No videos found yet in outputs/ or outputs_vids/.")
        else:
            c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
            c1.text_input("Path to an MP4 or WebM file", key="up_path",
                          placeholder=r"C:\Users\you\Videos\clip.mp4", label_visibility="collapsed")
            c2.button("Browse...", key="up_browse", on_click=browse_video, use_container_width=True)
            txt = st.session_state.get("up_path", "").strip().strip('"')
            if txt:
                cand = Path(txt).expanduser()
                if cand.is_file():
                    source = cand
                else:
                    st.warning("File not found.")
        if source:
            mb = source.stat().st_size / 1e6
            if source.suffix.lower() not in (".mp4", ".webm"):
                st.error("The site only accepts MP4 or WebM.")
                source = None
            elif mb > MAX_MB:
                st.error(f"{mb:.0f} MB is over the site's {MAX_MB} MB limit.")
                source = None

with right:
    with st.container(border=True):
        card_title("▶", "Preview")
        if source:
            show_video(source)
            st.caption(f"{source.name} · {source.stat().st_size / 1e6:.1f} MB (the site accepts up to {MAX_MB} MB and 5 minutes)")
        else:
            st.caption("Choose a video to preview it here.")

go = st.button("🔍  Upscale video", type="primary", use_container_width=True, disabled=running or source is None)
if go:
    if WORKER is None:
        st.error("upscale_work.py not found next to this app.")
    else:
        cmd = [sys.executable, str(WORKER), "--input", str(source), "--outdir", str(OUTDIR),
               "--quality", quality, "--timeout-min", str(int(timeout_min))]
        if resolution != RESOLUTIONS[0]:
            cmd += ["--resolution", resolution]
        if model != MODELS[0]:
            cmd += ["--model", model]
        if not show_browser:
            cmd.append("--headless")
        start_job(cmd, source)
        st.rerun()

# ---------------------------------------------------------------- live
if job and running:
    @st.fragment(run_every=2)
    def live_panel():
        j = JOBS["upscale"]
        if j["proc"].poll() is not None:
            st.rerun()
        mins, sec = divmod(int(time.time() - j["started"]), 60)
        with st.container(border=True):
            st.markdown(f'<div class="card-title"><span class="pulse"></span>Upscaling… {mins}:{sec:02d}</div>'
                        f'<div class="card-sub">Keep the opened browser window alive; the run continues if you refresh this page.</div>',
                        unsafe_allow_html=True)
            a, b = st.columns(2, gap="medium")
            with a:
                if not show_shot(LIVE_SHOT, j["started"], "Upscaler page"):
                    st.caption("The page screenshot appears once the file is loaded.")
            with b:
                with st.container(height=420):
                    st.code("\n".join(read_log(j).splitlines()[-80:]), language=None, wrap_lines=True)
            st.button("Stop run", on_click=stop_job, key="up_stop")

    live_panel()

# ---------------------------------------------------------------- result
if job and job["done"]:
    res = job["result"]
    if res and res.get("ok"):
        out = Path(res["files"][0])
        with st.container(border=True):
            st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
            card_title("🎞", "Original and upscaled")
            c1, c2 = st.columns(2, gap="large")
            with c1:
                st.caption("Original")
                if job["source"].exists():
                    show_video(job["source"])
            with c2:
                st.caption(f"Upscaled · {out.name}")
                if out.exists():
                    show_video(out)
                    st.download_button(f"Download {out.name}", out.read_bytes(), file_name=out.name, key="up_dl")
    else:
        err = (res or {}).get("error", "the worker stopped without a result (stopped by you, or it crashed)")
        st.markdown('<span class="chip bad">Run failed</span>', unsafe_allow_html=True)
        st.error(err)
    if FINAL_SHOT.exists() and FINAL_SHOT.stat().st_mtime >= job["started"] - 2:
        with st.expander("Page screenshot", expanded=not (res and res.get("ok"))):
            show_shot(FINAL_SHOT, job["started"], "Upscaler page at the end of the run")
    with st.expander("Run log", expanded=not (res and res.get("ok"))):
        st.code(read_log(job), language=None, wrap_lines=True)