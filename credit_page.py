"""Credit collector page for the Video studio app (Digen).

Needs credit_work.py and the shared helpers file (ui_common) next to it. Run alone with
    streamlit run credit_page.py
or add it to the combined app like the other pages.

Accounts come from the same places as the Digen page: cookies/*.json, acc*.json and
digen_cookies.json. Add or replace cookie files on the Digen page.
The worker runs in the background and writes one log (outputs_credits/logs/last_run.log) that is
overwritten at the start of every run. The page tails it, so a refresh does not stop the run.
"""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

# the shared look/helpers file: change this name if your file is called something else
import ui_common as ui

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_credits"
LOGDIR = OUTDIR / "logs"
SHOTS = OUTDIR / "shots"
COOKIES = BASE / "cookies"
for d in (OUTDIR, LOGDIR, SHOTS):
    d.mkdir(exist_ok=True)

WORKER = BASE / "credit_work.py"
LOG_FILE = LOGDIR / "last_run.log"
HISTORY = OUTDIR / "history.csv"
LIVE_LINES = 80
SHOWN_LINES = 3000
JOB_KEY = "credits"

ui.inject_css()


@st.cache_resource
def credit_job_store():
    """One dict for the whole server process: survives reruns, refreshes and new sessions."""
    return {}


JOBS = credit_job_store()


def list_cookie_files():
    """label -> Path, same sources as the Digen page."""
    found = {}
    if COOKIES.is_dir():
        for p in sorted(COOKIES.glob("*.json")):
            found[f"cookies/{p.name}"] = p
    for p in sorted(BASE.glob("acc*.json")):
        found[p.name] = p
    if (BASE / "digen_cookies.json").exists():
        found["digen_cookies.json"] = BASE / "digen_cookies.json"
    return found


# ---------------------------------------------------------------- job
def start_job(cmd, chosen):
    now = time.strftime("%H:%M:%S")
    cmd_txt = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    LOG_FILE.write_text(f"{now} [UI] worker: {WORKER}\n"
                        f"{now} [UI] accounts: {', '.join(chosen)}\n"
                        f"{now} [UI] command: {cmd_txt}\n", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(LOG_FILE, "ab") as fh:
        proc = subprocess.Popen(cmd, cwd=BASE, env=env, stdout=fh, stderr=subprocess.STDOUT)
    JOBS[JOB_KEY] = {"proc": proc, "log": LOG_FILE, "started": time.time(), "done": False, "result": None}


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
    st.header("Credit collector")
    mode = st.radio("What to do", ["Claim free credits, then report", "Only report the balances"],
                    key="cc_mode")
    headed = st.checkbox("Show the browser window while it works", value=False, key="cc_headed")
    verbose = st.checkbox("Verbose log (lists every button it sees)", value=False, key="cc_verbose")
    extra = st.text_input("Extra button words (optional)", key="cc_extra",
                          placeholder="e.g. free spin|bonus",
                          help="Words that mark a free-credit button, separated by |. Add them if the "
                               "collector says 'nothing to claim' but you know there is a button.")
    pause = st.number_input("Seconds between accounts", min_value=0.0, value=2.0, step=1.0, key="cc_pause")

# ---------------------------------------------------------------- hero
ui.hero("🪙 Digen credits", "Credit collector",
        "Visits every saved account through its cookies, claims the free credits, and tells you the "
        "balance before and after.")

job = JOBS.get(JOB_KEY)
running = bool(job) and job["proc"].poll() is None
if job and not running and not job["done"]:
    finalize(job)

available = list_cookie_files()
with st.container(border=True):
    ui.card_title("1", "Accounts", "Every cookie file found. Untick the ones you want to skip.")
    if not available:
        st.warning("No cookie files found. Add them on the Digen page (cookies folder, acc*.json "
                   "or digen_cookies.json).")
    chosen = st.multiselect("Accounts to visit", list(available), default=list(available), key="cc_accounts")
    if chosen:
        st.caption(f"{len(chosen)} account(s) selected.")

go = st.button("🪙  Collect credits", type="primary", use_container_width=True, disabled=running)

if go:
    problem = None
    if not WORKER.exists():
        problem = "credit_work.py not found. Put it next to this page."
    elif not chosen:
        problem = "Select at least one account."
    if problem:
        st.error(problem)
    else:
        cmd = [sys.executable, str(WORKER), "--cookies", *[str(available[k]) for k in chosen],
               "--outdir", str(OUTDIR), "--pause", str(pause)]
        if mode.startswith("Only"):
            cmd.append("--no-claim")
        if extra.strip():
            cmd += ["--claim-words", extra.strip()]
        if headed:
            cmd.append("--headed")
        if verbose:
            cmd.append("--verbose")
        start_job(cmd, chosen)
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
            st.markdown(f'<div class="card-title"><span class="pulse"></span>Collecting… {mins}:{sec:02d}</div>'
                        f'<div class="card-sub">You can refresh this page; the run keeps going.</div>',
                        unsafe_allow_html=True)
            with st.container(height=420):
                st.code("\n".join(ui.read_log(j).splitlines()[-LIVE_LINES:]), language=None, wrap_lines=True)
            st.button("Stop run", on_click=stop_job, key="cc_stop")

    live_panel()

# ---------------------------------------------------------------- result
if job and job["done"]:
    data = job["result"]
    if data and data.get("ok"):
        rows = data.get("accounts", [])
        with st.container(border=True):
            st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
            ui.card_title("🪙", "Result")
            st.markdown(
                f'<div class="facts">'
                f'<div class="fact"><b>{len(rows)}</b><span>accounts visited</span></div>'
                f'<div class="fact"><b>+{data.get("gained", 0)}</b><span>credits gained</span></div>'
                f'<div class="fact"><b>{data.get("total_after", 0)}</b><span>total credits now</span></div>'
                f'<div class="fact"><b>{data.get("seconds", 0)} s</b><span>time taken</span></div>'
                f'</div>', unsafe_allow_html=True)
            df = pd.DataFrame([{
                "Account": r["account"], "Before": r["before"], "After": r["after"], "Gained": r["gained"],
                "Status": r["status"], "Clicked": "; ".join(r["claimed"]), "Problem": r["error"]} for r in rows])
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button("Download this result (CSV)", df.to_csv(index=False), file_name="credits_result.csv",
                               key="cc_dl_res")
            odd = [r for r in rows if r["status"] in ("nothing to claim", "clicked, balance unchanged", "not logged in")]
            if odd:
                st.caption("Accounts that gained nothing: " + ", ".join(r["account"] for r in odd) +
                           ". Their screenshots below show what the page and the credits menu looked like.")
        with st.expander("Screenshots per account"):
            for r in rows:
                for suffix, cap in (("_menu", "credits menu"), ("", "page after"), ("_nologin", "not logged in"),
                                    ("_error", "error")):
                    shot = SHOTS / f"{r['account']}{suffix}.png"
                    if shot.exists() and shot.stat().st_mtime >= job["started"] - 2:
                        st.image(shot.read_bytes(), caption=f"{r['account']}: {cap}", use_container_width=True)
    else:
        err = (data or {}).get("error", "the worker stopped without a result (stopped by you, or it crashed)")
        st.markdown('<span class="chip bad">Run failed</span>', unsafe_allow_html=True)
        st.error(err if isinstance(err, str) else json.dumps(err, indent=2))

    with st.expander("Run log", expanded=not (data and data.get("ok"))):
        render_log(ui.read_log(job), job["log"].name, key="cc_dl_log")

# ---------------------------------------------------------------- history
if HISTORY.exists():
    with st.expander("History (every run, every account)"):
        try:
            hist = pd.read_csv(HISTORY)
            st.dataframe(hist.iloc[::-1], use_container_width=True, hide_index=True)
            st.download_button("Download history (CSV)", HISTORY.read_bytes(), file_name="credits_history.csv",
                               key="cc_dl_hist")
        except Exception as e:
            st.error(f"Could not read the history file: {e}")