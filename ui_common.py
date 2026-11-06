"""Shared look and small helpers for the extra pages (upscaler)."""
import json
import shutil
import subprocess
from pathlib import Path

import streamlit as st

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


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def hero(tag, title, text):
    st.markdown(
        f'''<div class="hero"><div class="reel"></div><span class="clap">{tag}</span>
        <h1>{title}</h1><p>{text}</p></div>''', unsafe_allow_html=True)


def card_title(num, title, sub=""):
    st.markdown(
        f'<div class="card-title"><span class="dot">{num}</span>{title}</div>'
        + (f'<div class="card-sub">{sub}</div>' if sub else ""),
        unsafe_allow_html=True,
    )


def read_log(job):
    try:
        return job["log"].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_result(text):
    for line in reversed(text.splitlines()):
        if line.startswith('{"ok"'):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return None


def show_shot(path, started, caption):
    try:
        if path.exists() and path.stat().st_mtime >= started - 2:
            st.image(path.read_bytes(), caption=caption, use_container_width=True)
            return True
    except OSError:
        pass
    return False


def playable(path):
    """H.264 + yuv420p files play as they are; others get a browser-friendly copy via ffmpeg."""
    path = Path(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or path.suffix.lower() == ".webm":
        return path
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,pix_fmt", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        out = r.stdout.strip().lower()
        if out.startswith("h264") and "yuv420p" in out:
            return path
    web = path.with_name(path.stem + "_web.mp4")
    if web.exists() and web.stat().st_size > 0:
        return web
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23", "-c:a", "aac",
                        "-movflags", "+faststart", str(web)], capture_output=True, text=True)
    return web if r.returncode == 0 and web.exists() else path


def show_video(path):
    with st.spinner("Preparing preview…"):
        vf = playable(path)
    st.video(str(vf))
    return vf