"""Google Flow page for the Video studio app (also runs alone: streamlit run flow_page.py).

Needs flow_work.py next to it.

Accounts are cookie files (EditThisCookie / Playwright JSON export), saved in cookies_flow/.
Add them in the sidebar by upload, paste or path. Export while signed in to Google and include
ALL cookies for google.com (not only flow.google.com).

The worker runs in the background and writes ONE log file (outputs_flow/logs/last_run.log),
overwritten at the start of every run. The page tails it, so refreshing keeps the live log.
Reference photos are read from where they are (paths, no copying).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

import flow_cookies as cm
from ui_common import (card_title, hero, inject_css, parse_result, read_log,
                       show_shot, show_video)

try:
    st.set_page_config(page_title="Google Flow", page_icon="🌊", layout="wide")
except Exception:
    pass

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_flow"
LOGDIR = OUTDIR / "logs"
COOKIES = BASE / "cookies_flow"
for d in (OUTDIR, LOGDIR, COOKIES):
    d.mkdir(exist_ok=True)
if not (COOKIES / ".gitignore").exists():           # keep session cookies out of Git
    (COOKIES / ".gitignore").write_text("*\n")

LIVE_SHOT = OUTDIR / "live.png"
FINAL_SHOT = OUTDIR / "final.png"
LOG_FILE = LOGDIR / "last_run.log"
WORKER = BASE / "flow_work.py"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_MB = 20
LIVE_LINES = 80
SHOWN_LINES = 3000
RATIOS = ["16:9", "9:16", "1:1", "4:3"]


@st.cache_resource
def job_store():
    """One dict for the whole server process: survives reruns, refreshes and new sessions."""
    return {}


JOBS = job_store()


# ---------------------------------------------------------------- file pickers
_DIALOGS = {
    "json": "path = filedialog.askopenfilename(title=%r, filetypes=[('JSON files', '*.json'), ('All files', '*.*')])",
    "images": ("path = filedialog.askopenfilenames(title=%r, filetypes=["
               "('Images', '*.png *.jpg *.jpeg *.webp'), ('All files', '*.*')])"),
}


def native_dialog(kind, title):
    """tkinter in its own process (it is unreliable inside Streamlit threads). Localhost only."""
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1)\n"
        + (_DIALOGS[kind] % title) + "\n"
        "root.destroy()\n"
        "print('\\n'.join(path) if isinstance(path, (tuple, list)) else (path or ''))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return r.stdout.strip()


def browse_json(key):
    p = native_dialog("json", "Select cookie file")
    if p:
        st.session_state[key] = str(Path(p))


def browse_images(key):
    picked = [x.strip() for x in native_dialog("images", "Select reference images").splitlines() if x.strip()]
    if picked:
        current = [x.strip() for x in st.session_state.get(key, "").splitlines() if x.strip()]
        merged = current + [str(Path(x)) for x in picked if str(Path(x)) not in current]
        st.session_state[key] = "\n".join(merged)


# ---------------------------------------------------------------- cookies
def save_cookie(name, data):
    return cm.save_cookie(COOKIES, name, data)


def flash_saved(path, missing):
    msg = f"Saved {path.name}."
    if missing:
        msg += " Warning: no Google login cookies (SID / __Secure-1PSID) found, export again while signed in."
    st.session_state.flash = msg


def list_cookie_files():
    return cm.list_cookie_files(COOKIES)


def check_cookies(files):
    """Headless check of which cookie files still give a signed-in Flow. Returns {name: bool} or None."""
    cmd = [sys.executable, str(WORKER), "--check", *[str(f) for f in files]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=BASE)
    except subprocess.TimeoutExpired:
        return None
    for line in reversed(r.stdout.splitlines()):
        if line.startswith('{"checked"'):
            try:
                return json.loads(line)["checked"]
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- prompt
def compose_prompt(text, ratio, n_images, mode="video"):
    base = text.strip().rstrip(". \n\t")
    if not base:
        return ""
    if mode == "character":
        return base
    s = f"{base}. Aspect ratio {ratio}."
    if mode == "video" and "video" not in base.lower():
        s += " Create a video (not still images)."
    if mode == "image" and "image" not in base.lower():
        s += " Generate images, not a video."
    if n_images == 1:
        s += " Use the attached image as the reference."
    elif n_images > 1:
        s += f" Use the {n_images} attached images as references."
    return s


# ---------------------------------------------------------------- logs / jobs
def start_job(cmd, chosen, prompt):
    now = time.strftime("%H:%M:%S")
    cmd_txt = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    LOG_FILE.write_text(                           # "w": the previous run's log is replaced
        f"{now} [UI] worker: {WORKER}\n"
        f"{now} [UI] accounts selected: {', '.join(chosen)}\n"
        f"{now} [UI] command: {cmd_txt}\n", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(LOG_FILE, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=fh, stderr=subprocess.STDOUT)
    JOBS["flow"] = {"proc": proc, "log": LOG_FILE, "started": time.time(),
                    "prompt": prompt, "done": False, "result": None}


def stop_job():
    job = JOBS.get("flow")
    if job and job["proc"].poll() is None:
        if os.name == "nt":                        # also kills the browser children
            subprocess.run(["taskkill", "/PID", str(job["proc"].pid), "/T", "/F"], capture_output=True)
        else:
            job["proc"].terminate()


def finalize(job):
    job["done"] = True
    job["result"] = parse_result(read_log(job))
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


# ---------------------------------------------------------------- state
job = JOBS.get("flow")
running = bool(job) and job["proc"].poll() is None
if job and not running and not job["done"]:
    finalize(job)

# ---------------------------------------------------------------- sidebar: accounts
st.session_state.setdefault("up_key", 0)

with st.sidebar:
    st.header("Accounts")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    tab_up, tab_paste, tab_path = st.tabs(["Upload", "Paste", "Path"])

    with tab_up:
        uploaded = st.file_uploader("Cookie files (.json)", type=["json"], accept_multiple_files=True,
                                    key=f"up{st.session_state.up_key}")
        if st.button("Save uploaded cookies", disabled=not uploaded):
            ok, errors = 0, []
            for f in uploaded:
                try:
                    path, missing = save_cookie(f.name, cm.parse_cookies(f.getvalue()))
                    flash_saved(path, missing)
                    ok += 1
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
            for e in errors:
                st.error(e)
            if ok and not errors:
                st.session_state.up_key += 1
                st.rerun()

    with tab_paste:
        pname = st.text_input("File name", value=f"acc{len(list_cookie_files()) + 1}.json")
        praw = st.text_area("Cookie JSON", height=140, placeholder="Paste the cookie export here")
        if st.button("Save pasted cookies", disabled=not praw.strip()):
            try:
                path, missing = save_cookie(pname, cm.parse_cookies(praw))
                flash_saved(path, missing)
                st.rerun()
            except Exception as e:
                st.error(f"Not valid cookie JSON: {e}")

    with tab_path:
        st.caption("Pick a cookie file from this computer, then import it.")
        c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
        c1.text_input("Path to a cookie file", key="cookie_path", placeholder=r"C:\Users\you\Desktop\acc1.json")
        c2.button("Browse...", on_click=browse_json, args=("cookie_path",), use_container_width=True)
        cpath = st.session_state.get("cookie_path", "").strip().strip('"')
        if st.button("Import cookie file", disabled=not cpath):
            src = Path(cpath).expanduser()
            try:
                if not src.is_file():
                    raise FileNotFoundError(f"file not found: {src}")
                path, missing = save_cookie(src.name, cm.parse_cookies(src.read_text(encoding="utf-8-sig")))
                flash_saved(path, missing)
                st.rerun()
            except Exception as e:
                st.error(str(e))

    available = list_cookie_files()
    chosen = st.multiselect(
        "Use these accounts (tried in order)", list(available), default=list(available),
        help="The first account that works is used. Later accounts are only tried if an earlier "
             "one fails BEFORE the generation is approved (cookies rejected, page not ready, too costly).")

    if st.button("Check sign-in", disabled=not chosen or running, use_container_width=True):
        with st.spinner("Checking…"):
            res = check_cookies([available[k] for k in chosen])
        if res is None:
            st.error("The check did not finish.")
        else:
            for n, ok in res.items():
                (st.success if ok else st.warning)(f"{n}: {'signed in' if ok else 'NOT signed in (expired or rejected)'}")

    if available:
        with st.expander("Remove a saved cookie file"):
            victim = st.selectbox("Saved file", list(available))
            if st.button("Delete", disabled=running):
                cm.delete_cookie(available[victim])
                st.rerun()

    st.header("Run options")
    headed = st.checkbox("Show the browser window while it works", value=False,
                         help="Tick this if headless runs get blocked by Google.")
    always_approve = st.checkbox("Choose 'Always approve' on the cost question", value=False)
    max_credits = st.number_input("Reject if a generation costs more than (0 = no limit)",
                                  min_value=0, value=0, step=10)
    timeout_min = st.number_input("Give up after (minutes)", min_value=5, value=30, step=5)
    project_url = st.text_input("Reuse a Flow project (optional)", placeholder="paste a project URL",
                                help="Leave empty to start a new project on every run.")
    verbose = st.checkbox("Verbose log", value=False)

# ---------------------------------------------------------------- main
inject_css()
hero("🌊 Google Flow", "Flow desk",
     "Describe the video, add reference photos if you want, and the script drives Flow for you.")

left, right = st.columns([1.15, 1], gap="large")

with left:
    with st.container(border=True):
        mode_label = st.radio("What to make", ["Video", "Images", "Character"], horizontal=True,
                              help="Video and Images both use the session chat; Character uses Flow's New character page.")
        mode = {"Video": "video", "Images": "image", "Character": "character"}[mode_label]
        card_title("1", "Describe the " + {"video": "video", "image": "images", "character": "character"}[mode],
                   {"video": "What should happen on screen.", "image": "What the pictures should show.", "character": "Who the character is: look, clothes, mood."}[mode])
        prompt = st.text_area("Prompt", height=130, label_visibility="collapsed",
                              placeholder="Make a promotional video for our online store, bright colors, fast cuts")
        ratio = st.radio("Aspect ratio", RATIOS, horizontal=True) if mode != "character" else RATIOS[0]

    with st.container(border=True):
        card_title("2", "Reference photos", "Optional. One path per line, or pick several with Browse. Nothing is copied.")
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        c1.text_area("Reference photo paths", key="image_paths", height=100, label_visibility="collapsed",
                     placeholder="C:\\Users\\you\\Pictures\\photo1.jpg\nC:\\Users\\you\\Pictures\\photo2.png")
        c2.button("Browse...", on_click=browse_images, args=("image_paths",), use_container_width=True)
        image_files, bad_paths = [], []
        for line in st.session_state.get("image_paths", "").splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            cand = Path(line).expanduser()
            if cand.is_file() and cand.suffix.lower() in IMAGE_EXTS:
                if cand.resolve() not in image_files:
                    image_files.append(cand.resolve())
            else:
                bad_paths.append(line)
        if bad_paths:
            st.warning("Not a PNG, JPG or WebP file: " + "; ".join(bad_paths))
        too_big = [f.name for f in image_files if f.stat().st_size > MAX_IMAGE_MB * 1024 * 1024]
        if too_big:
            st.warning(f"Over {MAX_IMAGE_MB} MB: " + ", ".join(too_big))

final_prompt = compose_prompt(prompt, ratio, len(image_files), mode)

with right:
    with st.container(border=True):
        card_title("▶", "Preview")
        if image_files:
            st.image([str(f) for f in image_files], caption=[f.name for f in image_files], width=100)
        else:
            st.caption("No reference photos.")
        card_title("✎", "Sent to Flow as")
        if final_prompt:
            st.code(final_prompt, language=None, wrap_lines=True)
        else:
            st.caption("Write a prompt and the full text appears here.")

go = st.button("🎬  Generate " + mode_label.lower(), type="primary", use_container_width=True,
               disabled=running)

if go:
    problem = None
    if not WORKER.exists():
        problem = "flow_work.py not found next to this page."
    elif not final_prompt:
        problem = "Write a prompt first."
    elif not chosen:
        problem = "Add a cookie file in the sidebar and select it."
    elif bad_paths:
        problem = "Fix or remove the photo paths that are not valid image files."
    elif too_big:
        problem = f"Remove the images over {MAX_IMAGE_MB} MB."

    if problem:
        st.error(problem)
    else:
        cmd = [sys.executable, str(WORKER), "--prompt", final_prompt, "--outdir", str(OUTDIR),
               "--cookies", *[str(available[k]) for k in chosen],
               "--mode", mode, "--timeout-min", str(int(timeout_min)), "--log-file", str(LOG_FILE)]
        if image_files:
            cmd += ["--image", *[str(f) for f in image_files]]
        if project_url.strip():
            cmd += ["--project-url", project_url.strip()]
        if max_credits:
            cmd += ["--max-credits", str(int(max_credits))]
        if always_approve:
            cmd.append("--always-approve")
        if headed:
            cmd.append("--headed")
        if verbose:
            cmd.append("--verbose")
        start_job(cmd, chosen, final_prompt)
        st.rerun()

# ---------------------------------------------------------------- live log
if job and running:
    @st.fragment(run_every=2)
    def live_panel():
        j = JOBS["flow"]
        if j["proc"].poll() is not None:
            st.rerun()
        mins, sec = divmod(int(time.time() - j["started"]), 60)
        with st.container(border=True):
            st.markdown(
                f'<div class="card-title"><span class="pulse"></span>Working… {mins}:{sec:02d}</div>'
                f'<div class="card-sub">You can refresh this page; the run keeps going.</div>',
                unsafe_allow_html=True)
            shot_col, log_col = st.columns([1, 1], gap="medium")
            with shot_col:
                if not show_shot(LIVE_SHOT, j["started"], "Flow page (refreshes about every 30 s)"):
                    st.caption("The page screenshot appears once the request is sent.")
            with log_col:
                with st.container(height=420):
                    st.code("\n".join(read_log(j).splitlines()[-LIVE_LINES:]), language=None, wrap_lines=True)
            st.button("Stop run", on_click=stop_job, key="stop_btn",
                      help="Stops the script. A generation already approved in Flow may keep running there.")

    live_panel()

# ---------------------------------------------------------------- result
if job and job["done"]:
    res = job["result"]
    if res and res.get("ok"):
        files = [f for f in (Path(x).resolve() for x in res.get("files", [])) if f.exists()]
        if files:
            with st.container(border=True):
                st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
                is_img = files[0].suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                card_title("🎞", "Your images" if is_img else "Your video")
                v_col, info_col = st.columns([1, 1], gap="large")
                with v_col:
                    if is_img:
                        st.image([str(f) for f in files], width=220)
                    else:
                        show_video(files[0])
                with info_col:
                    st.caption("Prompt used")
                    st.code(job["prompt"], language=None, wrap_lines=True)
                    for i, p in enumerate(files):
                        st.download_button(f"Download {p.name}", p.read_bytes(), file_name=p.name, key=f"dl{i}")
    else:
        st.markdown('<span class="chip bad">Run failed</span>', unsafe_allow_html=True)
        err = (res or {}).get("error", "the worker stopped without a result (stopped by you, or it crashed)")
        st.error(err if isinstance(err, str) else json.dumps(err, indent=2))

    if FINAL_SHOT.exists() and FINAL_SHOT.stat().st_mtime >= job["started"] - 2:
        with st.expander("Final page screenshot", expanded=not (res and res.get("ok"))):
            show_shot(FINAL_SHOT, job["started"], "Flow page at the end of the run")

    with st.expander("Run log", expanded=not (res and res.get("ok"))):
        render_log(read_log(job), job["log"].name, key="dl_log_last")