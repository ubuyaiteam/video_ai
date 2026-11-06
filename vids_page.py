"""Streamlit front end for the Google Vids worker ("Vids desk").

Run:   pip install "streamlit>=1.37" streamlit-local-storage
       streamlit run streamlit_app.py

Page of the combined app (vids_page.py, started from vids_app.py). Needs vids_work.py
and cookie_loader.py next to it.

Cookies are kept in the browser's local storage (not on disk). When you press Generate, the
selected accounts are written to a temporary folder for the worker, which is deleted when the
run ends.

The worker runs in the background and writes its output to ONE log file
(outputs/logs/last_run.log) that is overwritten at the start of every run. The page
tails that file every 2 seconds, so the live log keeps going when you refresh the
page, click a widget or switch tabs, and a finished run is still shown afterwards.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import streamlit as st
from streamlit_local_storage import LocalStorage

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_vids"
LOGDIR = OUTDIR / "logs"
for d in (OUTDIR, LOGDIR):
    d.mkdir(exist_ok=True)

LIVE_SHOT = OUTDIR / "live.png"                     # written by the worker while it waits
FINAL_SHOT = OUTDIR / "final.png"                   # written by the worker when generation ends
LOG_FILE = LOGDIR / "last_run.log"                  # the only log file; overwritten every run
LS_KEY = "vids_cookies"                             # local-storage item holding all accounts

WORKER = next((BASE / n for n in ("vids_work.py",) if (BASE / n).exists()), None)

RATIOS = {                                          # value: (name, w, h)
    "16:9": ("Landscape", 16, 9),
    "9:16": ("Portrait", 9, 16),
    "1:1": ("Square", 1, 1),
    "4:3": ("Standard", 4, 3),
    "21:9": ("Cinema", 21, 9),
}
RESOLUTIONS = ["720p", "1080p"]
LIVE_LINES = 80           # lines shown while a run is in progress
SHOWN_LINES = 3000        # lines shown in the finished-run log (download has all)
SEG_MAX = 8               # Veo in Vids makes one clip of about 8 s
CREDITS_PER_CLIP = 60     # ~50 credits per 10 s clip plus helper images


# ---------------------------------------------------------------- theme
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=DM+Sans:wght@400;500;600&display=swap');

:root {
  --ink: #141833;
  --muted: #5d6384;
  --paper: #eef0f8;
  --card: #ffffff;
  --line: #dde0f0;
  --accent: #4a3fe0;
  --accent-2: #ff6b57;
  --ok: #12a37f;
}
html, body, [class*="st-"], .stApp { font-family: 'DM Sans', system-ui, sans-serif; }
.stApp { background: var(--paper); color: var(--ink); }
h1, h2, h3, h4 { font-family: 'Bricolage Grotesque', 'DM Sans', sans-serif !important;
                 color: var(--ink); letter-spacing: -0.02em; }
.block-container { padding-top: 1.6rem; max-width: 1280px; }
header[data-testid="stHeader"] { background: transparent; }

/* hero */
.hero { position: relative; overflow: hidden; border-radius: 22px; padding: 30px 34px;
        background: linear-gradient(120deg, #1d1b5c 0%, #4a3fe0 55%, #8a5cf6 100%);
        color: #fff; margin-bottom: 22px; }
.hero h1 { color: #fff !important; font-size: 2.5rem; font-weight: 800; margin: 0 0 6px 0; }
.hero p { color: rgba(255,255,255,.82); font-size: 1.02rem; margin: 0; max-width: 620px; }
.hero .reel { position: absolute; right: -10px; top: 0; bottom: 0; width: 150px; opacity: .22;
              background: repeating-linear-gradient(to bottom, transparent 0 18px, #fff 18px 34px);
              -webkit-mask: linear-gradient(to right, transparent, #000); mask: linear-gradient(to right, transparent, #000); }
.hero .clap { display:inline-block; background: rgba(255,255,255,.16); border-radius: 999px;
              padding: 4px 12px; font-size: .82rem; margin-bottom: 12px; }

/* cards */
div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div[data-testid="stVerticalBlock"] .card-title) {
  background: var(--card); border: 1px solid var(--line) !important; border-radius: 18px;
  box-shadow: 0 6px 24px rgba(40, 40, 110, .06);
}
.card-title { display:flex; align-items:center; gap:10px; font-family:'Bricolage Grotesque',sans-serif;
              font-weight: 700; font-size: 1.15rem; margin-bottom: 4px; }
.card-title .dot { width: 26px; height: 26px; border-radius: 8px; background: var(--accent); color:#fff;
                   display:inline-flex; align-items:center; justify-content:center; font-size:.85rem; }
.card-sub { color: var(--muted); font-size: .9rem; margin-bottom: 10px; }

/* inputs */
.stTextArea textarea, .stTextInput input, .stNumberInput input {
  border-radius: 12px !important; border: 1px solid var(--line) !important; background: #fbfbff !important; }
.stTextArea textarea:focus, .stTextInput input:focus { border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(74,63,224,.15) !important; }

/* buttons */
.stButton > button, .stDownloadButton > button { border-radius: 12px; font-weight: 600;
  border: 1px solid var(--line); transition: transform .08s ease, box-shadow .15s ease; }
.stButton > button:hover { border-color: var(--accent); color: var(--accent); }
.stButton > button[kind="primary"] {
  background: linear-gradient(120deg, #4a3fe0, #7a4cf0); color: #fff; border: 0; height: 3.2rem;
  font-size: 1.05rem; box-shadow: 0 10px 24px rgba(74,63,224,.35); }
.stButton > button[kind="primary"]:hover { color:#fff; transform: translateY(-1px); }
.stButton > button[kind="primary"]:disabled { background: #b9b7ee; box-shadow: none; }

/* radios as pills */
div[role="radiogroup"] { gap: 8px; }
div[role="radiogroup"] > label { background: #f3f4fc; border: 1px solid var(--line); border-radius: 999px;
  padding: 4px 14px; }
div[role="radiogroup"] > label:has(input:checked) { background: var(--ink); border-color: var(--ink); }
div[role="radiogroup"] > label:has(input:checked) * { color: #fff !important; }
div[role="radiogroup"] > label > div:first-child { display: none; }

/* sidebar */
section[data-testid="stSidebar"] { background: #fff; border-right: 1px solid var(--line); }
section[data-testid="stSidebar"] h2 { font-size: 1.2rem; }

/* status chips and facts */
.chip { display:inline-flex; align-items:center; gap:6px; padding:4px 12px; border-radius:999px;
        font-size:.82rem; font-weight:600; }
.chip.ok { background:#dff6ee; color:#0b7a5d; }
.chip.bad { background:#ffe6e2; color:#b23a2b; }
.chip.run { background:#e6e4ff; color: var(--accent); }
.facts { display:flex; gap:10px; flex-wrap:wrap; margin: 6px 0 2px; }
.fact { background:#f3f4fc; border:1px solid var(--line); border-radius:12px; padding:8px 14px; }
.fact b { display:block; font-family:'Bricolage Grotesque',sans-serif; font-size:1.15rem; }
.fact span { color: var(--muted); font-size:.78rem; }

/* running banner */
.pulse { display:inline-block; width:10px; height:10px; border-radius:50%; background: var(--accent-2);
         margin-right:8px; animation: pulse 1.4s infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(255,107,87,.6);} 70% { box-shadow: 0 0 0 10px rgba(255,107,87,0);} 100% { box-shadow: 0 0 0 0 rgba(255,107,87,0);} }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }

/* code / log */
.stCodeBlock, pre { border-radius: 12px !important; }

/* ---- fixes: force light widgets, keep icon font ---- */
:root { color-scheme: light; }
[data-testid="stIconMaterial"], span[class*="material-symbols"], .material-icons {
  font-family: 'Material Symbols Rounded', 'Material Icons' !important; letter-spacing: normal !important; }
.stApp label, .stApp label p, [data-testid="stWidgetLabel"] p, .stApp p, .stApp li,
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stMarkdownContainer"] { color: var(--ink); }
.stApp [data-testid="stCaptionContainer"], .card-sub { color: var(--muted) !important; }
.hero p, .hero h1, .hero .clap { color: #fff !important; }
section[data-testid="stSidebar"], section[data-testid="stSidebar"] * { color: var(--ink); }
section[data-testid="stSidebar"] .chip.ok { color:#0b7a5d; }
section[data-testid="stSidebar"] .chip.bad { color:#b23a2b; }
/* buttons */
.stButton > button:not([kind="primary"]), .stDownloadButton > button {
  background: #fff !important; color: var(--ink) !important; border: 1px solid var(--line) !important; }
.stButton > button:not([kind="primary"]):hover { border-color: var(--accent) !important; color: var(--accent) !important; }
.stButton > button:not([kind="primary"]):disabled { opacity: .5; }
.stButton > button[kind="primary"] * { color:#fff !important; }
/* inputs, selects, uploader */
.stTextArea textarea, .stTextInput input, .stNumberInput input {
  background:#fbfbff !important; color: var(--ink) !important; }
.stTextArea textarea::placeholder, .stTextInput input::placeholder { color:#9a9fc0 !important; }
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"] {
  background:#fbfbff !important; color: var(--ink) !important; border-radius: 12px !important; }
[data-baseweb="select"] * { color: var(--ink); }
[data-baseweb="tag"] { background: var(--accent) !important; }
[data-baseweb="tag"] * { color:#fff !important; }
[data-baseweb="popover"] *, [data-baseweb="menu"] { background:#fff; color: var(--ink); }
[data-testid="stFileUploaderDropzone"] { background:#f3f4fc !important; border:1px dashed var(--line) !important;
  border-radius: 14px; }
[data-testid="stFileUploaderDropzone"] * { color: var(--ink) !important; }
[data-testid="stFileUploaderDropzone"] button { background:#fff !important; border:1px solid var(--line) !important; }
/* tabs, expanders, checkboxes */
button[data-baseweb="tab"] { color: var(--muted) !important; }
button[data-baseweb="tab"][aria-selected="true"] { color: var(--accent) !important; }
[data-testid="stExpander"] { background:#fff; border:1px solid var(--line); border-radius:14px; }
[data-testid="stExpander"] summary * { color: var(--ink) !important; }
[data-testid="stCheckbox"] label span { color: var(--ink) !important; }
[data-testid="stCheckbox"] [data-baseweb="checkbox"] > div:first-child { background:#fff; border-color:#b8bcdc; }
/* radio pills */
label[data-baseweb="radio"] { background:#f3f4fc; border:1px solid var(--line); border-radius:999px;
  padding: 4px 14px; margin-right: 0; }
label[data-baseweb="radio"] > div:first-child { display:none !important; }
label[data-baseweb="radio"] * { color: var(--ink) !important; }
label[data-baseweb="radio"]:has(input:checked) { background: var(--ink); border-color: var(--ink); }
label[data-baseweb="radio"]:has(input:checked) * { color:#fff !important; }
/* slider */
[data-testid="stSlider"] [data-testid="stTickBarMin"], [data-testid="stSlider"] [data-testid="stTickBarMax"] { color: var(--muted); }
[data-testid="stSlider"] [role="slider"] { background: var(--accent) !important; }
[data-testid="stMetricValue"], [data-testid="stMetricLabel"] * { color: var(--ink) !important; }

/* ---- round 2: dropdowns, number inputs, radios in any Streamlit version ---- */
[data-baseweb="select"] div, [data-baseweb="select"] input { background-color:#fbfbff !important; color: var(--ink) !important; }
[data-baseweb="select"] > div { border:1px solid var(--line) !important; border-radius:12px !important; }
[data-baseweb="select"] svg { fill: var(--muted) !important; color: var(--muted) !important; }
[data-baseweb="tag"], [data-baseweb="tag"] div, [data-baseweb="tag"] span { background: var(--accent) !important; color:#fff !important; }
[data-testid="stNumberInputContainer"], [data-testid="stNumberInputContainer"] > div { background:#fbfbff !important; border-radius:12px !important; }
[data-testid="stNumberInputContainer"] input { background:#fbfbff !important; color: var(--ink) !important; }
[data-testid="stNumberInputContainer"] button { background:#fff !important; color: var(--ink) !important; border:0 !important; }
[data-testid="stNumberInputContainer"] button svg { fill: var(--ink) !important; }
[data-testid="stRadio"] div[role="radiogroup"] > label { background:#f3f4fc; border:1px solid var(--line);
  border-radius:999px; padding:4px 14px; margin-right:0; }
[data-testid="stRadio"] div[role="radiogroup"] > label > div:first-child { display:none !important; }
[data-testid="stRadio"] div[role="radiogroup"] > label * { color: var(--ink) !important; }
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) { background: var(--ink); border-color: var(--ink); }
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) * { color:#fff !important; }
[data-testid="stTooltipIcon"] svg { fill: var(--muted) !important; color: var(--muted) !important; }

/* ---- round 3: selected value and option list must always be readable ---- */
[data-baseweb="select"], [data-baseweb="select"] * { color: #141833 !important; -webkit-text-fill-color: #141833 !important; opacity: 1 !important; }
[data-baseweb="select"] > div { background:#ffffff !important; border:1.5px solid #4a3fe0 !important; }
[data-baseweb="select"] div, [data-baseweb="select"] input, [data-baseweb="select"] span { background-color:#ffffff !important; }
[data-baseweb="tag"], [data-baseweb="tag"] * { background:#4a3fe0 !important; color:#ffffff !important; -webkit-text-fill-color:#ffffff !important; }
[data-baseweb="popover"], [data-baseweb="popover"] > div, [data-baseweb="menu"], ul[role="listbox"] { background:#ffffff !important; }
[data-baseweb="popover"] li, ul[role="listbox"] li, [role="option"], [role="option"] * {
  background:#ffffff !important; color:#141833 !important; -webkit-text-fill-color:#141833 !important; }
[data-baseweb="popover"] li:hover, [role="option"]:hover, [role="option"]:hover * { background:#eceaff !important; }
[role="option"][aria-selected="true"], [role="option"][aria-selected="true"] * {
  background:#4a3fe0 !important; color:#ffffff !important; -webkit-text-fill-color:#ffffff !important; font-weight:600; }
[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked), [data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) * {
  background:#4a3fe0 !important; border-color:#4a3fe0 !important; color:#ffffff !important; -webkit-text-fill-color:#ffffff !important; font-weight:600; }

/* ---- round 4: dropdown boxes in lavender instead of black ---- */
.stApp [data-baseweb="select"], .stApp [data-baseweb="select"] > div, .stApp [data-baseweb="select"] div,
.stApp [data-testid="stSelectbox"] div[data-baseweb], .stApp [data-testid="stMultiSelect"] div[data-baseweb] {
  background: #e9e7ff !important; background-color: #e9e7ff !important; background-image: none !important; }
.stApp [data-baseweb="select"] > div { border: 1.5px solid #4a3fe0 !important; border-radius: 12px !important; }
.stApp [data-baseweb="select"] *, .stApp [data-testid="stSelectbox"] *, .stApp [data-testid="stSelectbox"] input {
  color: #141833 !important; -webkit-text-fill-color: #141833 !important; opacity: 1 !important; font-weight: 600; }
.stApp [data-baseweb="select"] svg { fill: #4a3fe0 !important; color: #4a3fe0 !important; }
.stApp [data-baseweb="select"] [data-baseweb="tag"], .stApp [data-baseweb="select"] [data-baseweb="tag"] * {
  background: #4a3fe0 !important; color: #fff !important; -webkit-text-fill-color: #fff !important; }
/* radio circles: visible ring, purple when chosen */
.stApp [data-testid="stRadio"] label > div:first-child { background:#fff !important; border:2px solid #4a3fe0 !important; }
.stApp [data-testid="stRadio"] label:has(input:checked) > div:first-child { background:#4a3fe0 !important; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


@st.cache_resource
def job_store():
    """One dict for the whole server process: survives reruns, refreshes and new sessions."""
    return {}


JOBS = job_store()
ls = LocalStorage()


def card_title(num, title, sub=""):
    st.markdown(
        f'<div class="card-title"><span class="dot">{num}</span>{title}</div>'
        + (f'<div class="card-sub">{sub}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- path picker (cookie files)
# The dialog runs in its own process: tkinter is unreliable inside Streamlit's threads.
# Works when the browser and Streamlit run on the same PC (localhost).
_DIALOGS = {
    "json": ("path = filedialog.askopenfilename(title=%r, filetypes=["
             "('JSON files', '*.json'), ('All files', '*.*')])"),
}


def native_dialog(kind, title):
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


def browse(key, kind, title):
    """Button callback: runs before the rerun, so it may set the text box's value."""
    p = native_dialog(kind, title)
    if p:
        st.session_state[key] = str(Path(p))


def path_input(label, key, kind, dialog_title, placeholder="", help=None):
    c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
    c1.text_input(label, key=key, placeholder=placeholder, help=help)
    c2.button("Browse...", key=f"btn_{key}", on_click=browse,
              args=(key, kind, dialog_title), use_container_width=True)
    return st.session_state.get(key, "").strip().strip('"')


# ---------------------------------------------------------------- cookies (browser local storage)
def parse_cookies(raw):
    """Validate a cookie export (EditThisCookie / Playwright format). Returns a list."""
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if not isinstance(data, list) or not data:
        raise ValueError("expected a JSON list of cookies")
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            raise ValueError("each cookie needs a name and a value")
    return data


def safe_name(name):
    stem = re.sub(r"[^\w.-]+", "_", Path(name).stem).strip("._") or "cookies"
    return stem + ".json"


def _parse_store(raw):
    """What local storage returned (a JSON string or an already-parsed dict) -> dict or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def load_store():
    """Accounts as {file name: cookie list}. The session copy wins once it exists, so a save or a
    delete shows up immediately; otherwise it is read from the browser (empty on the very first
    render, then the component triggers one automatic rerun with the real value)."""
    if "store" not in st.session_state:
        data = _parse_store(ls.getItem(LS_KEY))
        if data is not None:
            st.session_state["store"] = data
    return st.session_state.get("store", {})


def commit_store(accounts):
    """Change the session copy now and queue the browser write for the next render."""
    st.session_state["store"] = accounts
    st.session_state["pending_write"] = True


def flush_store():
    """Write to local storage. Must run in a normal render (not right before st.rerun), because the
    write happens when the component draws in the browser."""
    if st.session_state.pop("pending_write", False):
        n = st.session_state.get("write_n", 0) + 1
        st.session_state["write_n"] = n
        ls.setItem(LS_KEY, json.dumps(st.session_state.get("store", {})), key=f"set_{n}")


def save_cookie(name, data):
    accounts = dict(load_store())
    accounts[safe_name(name)] = data
    commit_store(accounts)


def delete_cookie(name):
    accounts = dict(load_store())
    accounts.pop(name, None)
    commit_store(accounts)


def materialize(chosen, accounts):
    """Write the selected accounts to a temporary folder for the worker. Returns (folder, paths)."""
    tmp = Path(tempfile.mkdtemp(prefix="vids_cookies_"))
    paths = []
    for name in chosen:
        p = tmp / name
        p.write_text(json.dumps(accounts[name]), encoding="utf-8")
        paths.append(str(p))
    return tmp, paths


# ---------------------------------------------------------------- prompt
def compose_prompt(text, ratio, res, secs):
    base = text.strip().rstrip(". \n\t")
    if not base:
        return ""
    return f"{base}. Aspect ratio {ratio} ({RATIOS[ratio][0].lower()})."


def frame_preview(ratio, res, secs):
    _, w, h = RATIOS[ratio]
    max_w = int(240 * w / h)
    st.markdown(
        f"""
        <div style="background:linear-gradient(160deg,#141833,#242a5c);border-radius:16px;padding:18px;
                    display:flex;justify-content:center;align-items:center;min-height:200px">
          <div style="aspect-ratio:{w}/{h};width:min(100%,{max_w}px);background:#1d2350;
                      border:2px solid rgba(255,255,255,.55);border-radius:6px;position:relative;
                      background-image:
                        linear-gradient(to right,transparent 33.2%,rgba(255,255,255,.18) 33.3%,transparent 33.5%,transparent 66.5%,rgba(255,255,255,.18) 66.6%,transparent 66.8%),
                        linear-gradient(to bottom,transparent 33.2%,rgba(255,255,255,.18) 33.3%,transparent 33.5%,transparent 66.5%,rgba(255,255,255,.18) 66.6%,transparent 66.8%);">
            <span style="position:absolute;left:8px;bottom:6px;color:#fff;font-size:12px;
                         background:rgba(0,0,0,.35);padding:2px 8px;border-radius:6px">{ratio} &middot; {res} &middot; {secs} s</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- video preview
def playable(path):
    """Return a file the browser can play.

    If the video is already H.264 + yuv420p it is returned unchanged. Otherwise a
    browser-friendly copy (<name>_web.mp4) is made once with ffmpeg and reused.
    Without ffmpeg the original file is returned.
    """
    path = Path(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or path.suffix.lower() == ".webm":
        return path
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        out = r.stdout.strip().lower()
        if out.startswith("h264") and "yuv420p" in out:
            return path
    web = path.with_name(path.stem + "_web.mp4")
    if web.exists() and web.stat().st_size > 0:
        return web
    r = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
         "-c:a", "aac", "-movflags", "+faststart", str(web)],
        capture_output=True, text=True)
    return web if r.returncode == 0 and web.exists() else path


def show_video(path):
    with st.spinner("Preparing preview…"):
        vf = playable(path)
    st.video(str(vf))
    return vf


# ---------------------------------------------------------------- logs
def render_log(text, name, key):
    """Scrollable log box plus a download button for the complete text."""
    rows = text.splitlines()
    shown = rows[-SHOWN_LINES:]
    if len(rows) > len(shown):
        st.caption(f"Showing the last {len(shown)} of {len(rows)} lines. Download for the full log.")
    with st.container(height=450):
        st.code("\n".join(shown), language=None, wrap_lines=True)
    st.download_button("Download log", text, file_name=name, key=key)


def show_shot(path, started, caption):
    """Show a worker screenshot if it was written during the current run."""
    try:
        if path.exists() and path.stat().st_mtime >= started - 2:
            st.image(path.read_bytes(), caption=caption, use_container_width=True)
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- background job
def parse_result(text):
    """The worker prints its result as one JSON line starting with {"ok"."""
    for line in reversed(text.splitlines()):
        if line.startswith('{"ok"'):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


def read_log(job):
    try:
        return job["log"].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def start_job(cmd, chosen, prompt, tmp_dir):
    """Start the worker detached from this script run.

    Its output goes to the single log file, which is replaced at the start of every run.
    tmp_dir holds the temporary cookie files and is deleted when the run ends.
    """
    now = time.strftime("%H:%M:%S")
    cmd_txt = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    LOG_FILE.write_text(                           # "w": the previous run's log is replaced
        f"{now} [UI] worker: {WORKER}\n"
        f"{now} [UI] accounts selected: {', '.join(chosen)}\n"
        f"{now} [UI] command: {cmd_txt}\n", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(LOG_FILE, "ab") as fh:               # the child keeps its own handle
        proc = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=fh, stderr=subprocess.STDOUT)
    JOBS["vids"] = {"proc": proc, "log": LOG_FILE, "started": time.time(),
                    "prompt": prompt, "done": False, "result": None, "tmp_dir": tmp_dir}


def stop_job():
    job = JOBS.get("vids")
    if job and job["proc"].poll() is None:
        if os.name == "nt":                        # also kills the Chromium children
            subprocess.run(["taskkill", "/PID", str(job["proc"].pid), "/T", "/F"], capture_output=True)
        else:
            job["proc"].terminate()


def finalize(job):
    """Called once, after the worker process has exited."""
    job["done"] = True
    job["result"] = parse_result(read_log(job))
    shutil.rmtree(job.get("tmp_dir") or "", ignore_errors=True)   # remove the temporary cookie files
    with open(job["log"], "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} [UI] worker exited with code "
                f"{job['proc'].returncode}; {time.time() - job['started']:.0f} s total; "
                f"temporary cookie files removed\n")


# ---------------------------------------------------------------- state
st.session_state.setdefault("up_key", 0)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Accounts")
    st.caption("Cookies are kept in this browser's local storage, not on disk.")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    available = load_store()

    tab_up, tab_paste, tab_path = st.tabs(["Upload", "Paste", "Path"])

    with tab_up:
        uploaded = st.file_uploader(
            "Cookie files (.json)", type=["json"], accept_multiple_files=True,
            key=f"up{st.session_state.up_key}",
        )
        if st.button("Save uploaded cookies", disabled=not uploaded):
            ok, errors = 0, []
            for f in uploaded:
                try:
                    save_cookie(f.name, parse_cookies(f.getvalue()))
                    ok += 1
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
            for e in errors:
                st.error(e)
            if ok and not errors:
                st.session_state.up_key += 1
                st.session_state.flash = f"Saved {ok} cookie file(s)."
                st.rerun()

    with tab_paste:
        n = len(available) + 1
        pname = st.text_input("File name", value=f"acc{n}.json")
        praw = st.text_area("Cookie JSON", height=140,
                            placeholder="Paste the EditThisCookie export here")
        if st.button("Save pasted cookies", disabled=not praw.strip()):
            try:
                save_cookie(pname, parse_cookies(praw))
                st.session_state.flash = f"Saved {safe_name(pname)}."
                st.rerun()
            except Exception as e:
                st.error(f"Not valid cookie JSON: {e}")

    with tab_path:
        st.caption("Pick a cookie file from this computer, then import it.")
        cpath = path_input("Path to a cookie file", "cookie_path", "json",
                           "Select cookie file", placeholder=r"C:\Users\you\Desktop\acc1.json")
        if st.button("Import cookie file", disabled=not cpath):
            src = Path(cpath).expanduser()
            try:
                if not src.is_file():
                    raise FileNotFoundError(f"file not found: {src}")
                save_cookie(src.name, parse_cookies(src.read_text(encoding="utf-8-sig")))
                st.session_state.flash = f"Imported {safe_name(src.name)}."
                st.rerun()
            except Exception as e:
                st.error(str(e))

    available = load_store()
    chosen = st.multiselect(
        "Use these accounts (tried in order)", list(available), default=list(available),
        help="Google accounts are tried in order. Later accounts are only used if an earlier one "
             "fails BEFORE the video is requested (expired cookies, usage limit...).",
    )

    if available:
        with st.expander("Remove a saved cookie file"):
            victim = st.selectbox("Saved file", list(available))
            if st.button("Delete"):
                delete_cookie(victim)
                st.session_state.flash = f"Deleted {victim}."
                st.rerun()

    # Write to the browser last, in a normal render. The save/delete buttons above set a flag and
    # rerun; this line runs on that rerun and the browser stores the new value.
    flush_store()

    st.header("Run options")
    open_vscode = False
    headed = st.checkbox("Show the browser window while it works", value=False)
    allow_unknown = False
    verbose = st.checkbox("Verbose log (every network request and console message)", value=False)
    min_credits = 0
    timeout_min = st.number_input("Give up after (minutes)", min_value=5, value=20, step=5,
                                  help="A Vids clip usually takes 2-5 minutes.")

# ---------------------------------------------------------------- hero
st.markdown(
    """
    <div class="hero">
      <div class="reel"></div>
      <span class="clap">🎬 Google Vids · Veo</span>
      <h1>Vids desk</h1>
      <p>Describe the shot, pick the frame and length, and the agent films it.
         Your choices are written into the prompt it receives.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

job = JOBS.get("vids")
running = bool(job) and job["proc"].poll() is None
if job and not running and not job["done"]:
    finalize(job)

left, right = st.columns([1.15, 1], gap="large")

with left:
    with st.container(border=True):
        card_title("1", "Describe the shot", "What happens on screen, the camera move and the light.")
        prompt = st.text_area(
            "Shot description", height=130, label_visibility="collapsed",
            placeholder="A lone astronaut walks across a red desert at sunrise, slow dolly-in, warm light over the dunes",
        )

    with st.container(border=True):
        card_title("2", "Frame and length")
        ratio = st.radio(
            "Screen size", list(RATIOS), horizontal=True,
            format_func=lambda r: f"{r} {RATIOS[r][0]}",
        )
        res = "Veo"
        secs = SEG_MAX
        clips = 1
        st.caption("Google Vids makes one clip of about 8 seconds per prompt. Daily limits apply to your Google account.")

final_prompt = compose_prompt(prompt, ratio, res, secs)

with right:
    with st.container(border=True):
        card_title("▶", "Preview")
        frame_preview(ratio, res, secs)
        est_min = "2-5 min"
        st.markdown(
            f'<div class="facts">'
            f'<div class="fact"><b>{clips}</b><span>clip{"s" if clips != 1 else ""}</span></div>'
            f'<div class="fact"><b>~{secs}s</b><span>clip length</span></div>'
            f'<div class="fact"><b>{est_min}</b><span>wait</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with st.container(border=True):
        card_title("✎", "Sent to the agent as")
        if final_prompt:
            st.code(final_prompt, language=None, wrap_lines=True)
        else:
            st.caption("Write a shot description and the full prompt appears here.")

go = st.button("🎬  Generate video", type="primary", use_container_width=True, disabled=running)

if go:
    problem = None
    if WORKER is None:
        problem = "No worker script found. Put vids_work.py next to streamlit_app.py."
    elif not final_prompt:
        problem = "Write a shot description first."
    elif not chosen:
        problem = "Upload or select at least one cookie file in the sidebar."

    if problem:
        st.error(problem)
    else:
        need = max(int(min_credits), CREDITS_PER_CLIP * clips) if secs > SEG_MAX else int(min_credits)
        tmp_dir, cookie_paths = materialize(chosen, available)
        cmd = [sys.executable, str(WORKER), "--prompt", final_prompt, "--outdir", str(OUTDIR),
               "--cookies", *cookie_paths,
               "--min-credits", str(need), "--seconds", str(secs),
               "--timeout-min", str(int(timeout_min))]
        if not open_vscode:
            cmd.append("--no-open")
        if headed:
            cmd.append("--headed")
        if allow_unknown:
            cmd.append("--allow-unknown-credits")
        if verbose:
            cmd.append("--verbose")
        start_job(cmd, chosen, final_prompt, tmp_dir)
        st.rerun()

# ---------------------------------------------------------------- live log
# A fragment that re-runs by itself every 2 s and only reads the log file, so it keeps
# updating no matter what else happens on the page.
if job and running:
    @st.fragment(run_every=2)
    def live_panel():
        j = JOBS["vids"]
        if j["proc"].poll() is not None:
            st.rerun()                             # finished: redraw the whole page with the result
        mins, sec = divmod(int(time.time() - j["started"]), 60)
        with st.container(border=True):
            st.markdown(
                f'<div class="card-title"><span class="pulse"></span>Filming… {mins}:{sec:02d}</div>'
                f'<div class="card-sub">You can refresh this page; the run keeps going.</div>',
                unsafe_allow_html=True,
            )
            shot_col, log_col = st.columns([1, 1], gap="medium")
            with shot_col:
                if not show_shot(LIVE_SHOT, j["started"], "Generation page (refreshes about every 30 s)"):
                    st.caption("The generation page screenshot appears once the request is sent.")
            rows = read_log(j).splitlines()
            with log_col:
                with st.container(height=420):
                    st.code("\n".join(rows[-LIVE_LINES:]), language=None, wrap_lines=True)
            st.button("Stop run", on_click=stop_job, key="stop_btn",
                      help="Stops the script. A request already sent to Google Vids may keep running there.")

    live_panel()

# ---------------------------------------------------------------- result
if job and job["done"]:
    res_data = job["result"]
    if res_data and res_data.get("ok"):
        files = [f for f in (Path(x).resolve() for x in res_data.get("files", [])) if f.exists()]
        if files:
            with st.container(border=True):
                st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
                card_title("🎞", "Your video")
                v_col, info_col = st.columns([1, 1], gap="large")
                with v_col:
                    show_video(files[0])
                with info_col:
                    if res_data.get("credits_after") is not None:
                        st.metric("Credits left on that account", res_data["credits_after"])
                    st.caption("Prompt used")
                    st.code(job["prompt"], language=None, wrap_lines=True)
                    for i, p in enumerate(files):
                        st.download_button(f"Download {p.name}", p.read_bytes(),
                                           file_name=p.name, key=f"dl{i}")
    else:
        err = (res_data or {}).get("error", "the worker stopped without a result (stopped by you, or it crashed)")
        st.markdown('<span class="chip bad">Run failed</span>', unsafe_allow_html=True)
        st.error(err if isinstance(err, str) else json.dumps(err, indent=2))

    if FINAL_SHOT.exists() and FINAL_SHOT.stat().st_mtime >= job["started"] - 2:
        with st.expander("Generation page screenshot", expanded=True):
            show_shot(FINAL_SHOT, job["started"], "Page where the video was generated")
            st.download_button("Download screenshot", FINAL_SHOT.read_bytes(),
                               file_name="generation_page.png", key="dl_shot")

    with st.expander("Run log", expanded=not (res_data and res_data.get("ok"))):
        render_log(read_log(job), job["log"].name, key="dl_log_last")