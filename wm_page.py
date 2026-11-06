"""Watermark remover page for the Video studio app (geminiwatermarkremover.io/video).

Needs wm_work.py and the shared helpers module (see the import below) next to it.
Add it to the combined app like the other pages, or run alone:
    streamlit run wm_page.py

The worker runs in the background and writes ONE log file (outputs_wm/logs/last_run.log)
that is overwritten at the start of every run. The page tails it every 2 seconds, so the
live log survives a refresh. Videos are read from where they are; nothing is copied.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

# the shared look/helpers file you uploaded: change this name if your file is called something else
import ui_common as ui

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_wm"
LOGDIR = OUTDIR / "logs"
for d in (OUTDIR, LOGDIR):
    d.mkdir(exist_ok=True)

WORKER = BASE / "wm_work.py"
LIVE_SHOT = OUTDIR / "live.png"
FINAL_SHOT = OUTDIR / "final.png"
LOG_FILE = LOGDIR / "last_run.log"
VIDEO_EXTS = {".mp4", ".webm", ".mov"}
SOFT_LIMIT_MB = 100
LIVE_LINES = 80
SHOWN_LINES = 3000
JOB_KEY = "wm"

ui.inject_css()


@st.cache_resource
def wm_job_store():
    """One dict for the whole server process: survives reruns, refreshes and new sessions."""
    return {}


JOBS = wm_job_store()


# ---------------------------------------------------------------- file picking
def native_dialog_videos(title):
    """tkinter dialog in its own process (works when the browser and Streamlit share a PC)."""
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1)\n"
        f"path = filedialog.askopenfilenames(title={title!r}, filetypes=["
        "('Videos', '*.mp4 *.webm *.mov'), ('All files', '*.*')])\n"
        "root.destroy()\n"
        "print('\\n'.join(path))\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()


def browse_videos(key):
    picked = [x.strip() for x in native_dialog_videos("Select videos to clean").splitlines() if x.strip()]
    if picked:
        current = [x.strip() for x in st.session_state.get(key, "").splitlines() if x.strip()]
        merged = current + [str(Path(x)) for x in picked if str(Path(x)) not in current]
        st.session_state[key] = "\n".join(merged)


def recent_videos(limit=40):
    """Videos made by the other tools (any outputs* folder), newest first, excluding this page's own output."""
    found = []
    for folder in BASE.glob("outputs*"):
        if not folder.is_dir() or folder.resolve() == OUTDIR.resolve():
            continue
        for p in folder.rglob("*"):
            if p.suffix.lower() in VIDEO_EXTS and not p.stem.endswith("_web"):
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    pass
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found[:limit]]


# ---------------------------------------------------------------- job
def start_job(cmd, videos):
    now = time.strftime("%H:%M:%S")
    cmd_txt = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    LOG_FILE.write_text(f"{now} [UI] worker: {WORKER}\n"
                        f"{now} [UI] videos: {', '.join(v.name for v in videos)}\n"
                        f"{now} [UI] command: {cmd_txt}\n", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(LOG_FILE, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=fh, stderr=subprocess.STDOUT)
    JOBS[JOB_KEY] = {"proc": proc, "log": LOG_FILE, "started": time.time(),
                     "videos": [str(v) for v in videos], "done": False, "result": None}


def stop_job():
    job = JOBS.get(JOB_KEY)
    if job and job["proc"].poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(job["proc"].pid), "/T", "/F"], capture_output=True)
        else:
            job["proc"].terminate()


def finalize(job):
    job["done"] = True
    job["result"] = ui.parse_result(ui.read_log(job))
    with open(job["log"], "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} [UI] worker exited with code "
                f"{job['proc'].returncode}; {time.time() - job['started']:.0f} s total\n")


def render_log(text, name, key):
    rows = text.splitlines()
    shown = rows[-SHOWN_LINES:]
    if len(rows) > len(shown):
        st.caption(f"Showing the last {len(shown)} of {len(rows)} lines. Download for the full log.")
    with st.container(height=450):
        st.code("\n".join(shown), language=None, wrap_lines=True)
    st.download_button("Download log", text, file_name=name, key=key)


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Watermark remover")
    headed = st.checkbox("Show the browser window while it works", value=False, key="wm_headed")
    verbose = st.checkbox("Verbose log", value=False, key="wm_verbose")
    timeout_min = st.number_input("Give up after (minutes, per video)", min_value=2, value=20, step=2,
                                  key="wm_timeout",
                                  help="The site cleans in the browser at the source resolution, "
                                       "so long clips take longer.")

# ---------------------------------------------------------------- hero
ui.hero("🧽 Gemini / Veo watermark remover", "Clean desk",
        "Pick your videos and the agent runs the free local cleanup on geminiwatermarkremover.io, "
        "then saves the clean files.")

job = JOBS.get(JOB_KEY)
running = bool(job) and job["proc"].poll() is None
if job and not running and not job["done"]:
    finalize(job)

with st.container(border=True):
    ui.card_title("1", "Pick videos",
                  "From your outputs folders, or paste paths (one per line), or Browse. MP4, WebM or MOV.")
    recent = recent_videos()
    from_outputs = st.multiselect("Recent videos from your other tools", recent, key="wm_recent",
                                  format_func=lambda p: f"{p.parent.name}/{p.name}")
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    c1.text_area("Video paths", key="wm_paths", height=100, label_visibility="collapsed",
                 placeholder="C:\\Users\\you\\Videos\\clip1.mp4\nC:\\Users\\you\\Videos\\clip2.mp4")
    c2.button("Browse...", key="wm_browse", on_click=browse_videos, args=("wm_paths",),
              use_container_width=True, help="Select several videos at once")

    videos, bad = [], []
    for p in from_outputs:
        if p.resolve() not in videos:
            videos.append(p.resolve())
    for line in st.session_state.get("wm_paths", "").splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        cand = Path(line).expanduser()
        if cand.is_file() and cand.suffix.lower() in VIDEO_EXTS:
            if cand.resolve() not in videos:
                videos.append(cand.resolve())
        else:
            bad.append(line)
    if bad:
        st.warning("Not an MP4, WebM or MOV file: " + "; ".join(bad))
    big = [v.name for v in videos if v.stat().st_size > SOFT_LIMIT_MB * 1024 * 1024]
    if big:
        st.warning(f"Over {SOFT_LIMIT_MB} MB (the site works best under that): " + ", ".join(big))
    if videos:
        st.caption(f"{len(videos)} video(s) selected.")

go = st.button("🧽  Remove watermark", type="primary", use_container_width=True, disabled=running)

if go:
    problem = None
    if not WORKER.exists():
        problem = "wm_work.py not found. Put it next to this page."
    elif not videos:
        problem = "Pick at least one video."
    elif bad:
        problem = "Fix or remove the paths that are not valid video files."
    if problem:
        st.error(problem)
    else:
        cmd = [sys.executable, str(WORKER), "--input", *[str(v) for v in videos],
               "--outdir", str(OUTDIR), "--timeout-min", str(int(timeout_min))]
        if headed:
            cmd.append("--headed")
        if verbose:
            cmd.append("--verbose")
        start_job(cmd, videos)
        st.rerun()

# ---------------------------------------------------------------- live log
if job and running:
    @st.fragment(run_every=2)
    def live_panel():
        j = JOBS[JOB_KEY]
        if j["proc"].poll() is not None:
            st.rerun()
        mins, sec = divmod(int(time.time() - j["started"]), 60)
        with st.container(border=True):
            st.markdown(f'<div class="card-title"><span class="pulse"></span>Cleaning… {mins}:{sec:02d}</div>'
                        f'<div class="card-sub">You can refresh this page; the run keeps going.</div>',
                        unsafe_allow_html=True)
            shot_col, log_col = st.columns([1, 1], gap="medium")
            with shot_col:
                if not ui.show_shot(LIVE_SHOT, j["started"], "Site view (refreshes about every 15 s)"):
                    st.caption("The screenshot appears once the video is loaded.")
            with log_col:
                with st.container(height=420):
                    st.code("\n".join(ui.read_log(j).splitlines()[-LIVE_LINES:]), language=None, wrap_lines=True)
            st.button("Stop run", on_click=stop_job, key="wm_stop")

    live_panel()

# ---------------------------------------------------------------- result
if job and job["done"]:
    data = job["result"]
    if data and data.get("ok"):
        files = [f for f in (Path(x).resolve() for x in data.get("files", [])) if f.exists()]
        with st.container(border=True):
            st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
            ui.card_title("🎞", "Clean videos")
            for i, f in enumerate(files):
                v_col, info_col = st.columns([1, 1], gap="large")
                with v_col:
                    ui.show_video(f)
                with info_col:
                    st.caption(f.name)
                    st.download_button(f"Download {f.name}", f.read_bytes(), file_name=f.name, key=f"wm_dl{i}")
        for src, err in (data.get("failed") or {}).items():
            st.error(f"{Path(src).name}: {err}")
    else:
        err = (data or {}).get("error", "the worker stopped without a result (stopped by you, or it crashed)")
        st.markdown('<span class="chip bad">Run failed</span>', unsafe_allow_html=True)
        st.error(err if isinstance(err, str) else json.dumps(err, indent=2))

    if FINAL_SHOT.exists() and FINAL_SHOT.stat().st_mtime >= job["started"] - 2:
        with st.expander("Last page screenshot"):
            ui.show_shot(FINAL_SHOT, job["started"], "Site after the cleanup")

    with st.expander("Run log", expanded=not (data and data.get("ok"))):
        render_log(ui.read_log(job), job["log"].name, key="wm_dl_log")