"""Edit page for the Video studio app: a Google Vids style timeline with drag and drop.

Next to this file you need:  edit_tools.py, timeline_widget.py, ui_common.py (the shared look) and the folder
timeline_component/ (with index.html). ffmpeg must be installed; `pip install edge-tts` gives the voices.
Add it to the combined app like the other pages, or run alone:  streamlit run edit_page.py

Videos are read from where they are. Files dropped into the uploader are saved to outputs_edit/uploads.
Results go to outputs_edit/.
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import streamlit as st

import edit_tools as et
from timeline_widget import timeline
# the shared look/helpers file: change this name if your file is called something else
import ui_common as ui

BASE = Path(__file__).parent.resolve()
OUTDIR = BASE / "outputs_edit"
TMPDIR = OUTDIR / "tmp"
UPLOADS = OUTDIR / "uploads"
for d in (OUTDIR, TMPDIR, UPLOADS):
    d.mkdir(exist_ok=True)

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
SIZES = {"Match the first clip": None, "1920 x 1080 (landscape)": (1920, 1080),
         "1280 x 720 (landscape)": (1280, 720), "1080 x 1920 (portrait)": (1080, 1920),
         "1080 x 1080 (square)": (1080, 1080)}

ui.inject_css()
ui.hero("✂️ Video editor", "Edit desk",
        "Drag videos from the media bin onto the timeline, reorder the scenes, cut them, set the sound "
        "and add different voices.")

if not et.tools_ok():
    st.error("ffmpeg and ffprobe were not found. Install them (winget install Gyan.FFmpeg), then restart the "
             "terminal and the app. You can also set FFMPEG_DIR to the folder that contains ffmpeg.exe.")
    st.stop()

st.session_state.setdefault("ed_clips", [])      # scenes on the timeline (no thumbnails stored here)
st.session_state.setdefault("ed_ver", 0)         # bumped when Python changes the list, so the timeline reloads
st.session_state.setdefault("ed_up_n", 0)
st.session_state.setdefault("ed_nlines", 1)
st.session_state.setdefault("ed_seen_uploads", set())


@st.cache_data(show_spinner=False)
def voice_list():
    return et.list_voices()


@st.cache_data(show_spinner=False)
def clip_info(path, mtime):
    """Duration, sound flag and thumbnail of a video (cached per file version). None if unreadable."""
    try:
        info = et.probe(path)
    except et.EditError:
        return None
    return {"path": path, "name": Path(path).name, "dur": info["duration"], "audio": info["has_audio"],
            "width": info["width"], "height": info["height"], "thumb": et.thumbnail(path)}


def info_for(path):
    try:
        return clip_info(str(path), os.path.getmtime(path))
    except OSError:
        return None


# ---------------------------------------------------------------- picking files
def native_dialog(title, filetypes):
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost', 1)\n"
        f"path = filedialog.askopenfilenames(title={title!r}, filetypes={filetypes!r})\n"
        "root.destroy()\n"
        "print('\\n'.join(path))\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()


def browse_videos(key):
    picked = [x.strip() for x in native_dialog(
        "Select videos", [("Videos", "*.mp4 *.webm *.mov *.mkv"), ("All files", "*.*")]).splitlines() if x.strip()]
    if picked:
        cur = [x.strip() for x in st.session_state.get(key, "").splitlines() if x.strip()]
        st.session_state[key] = "\n".join(cur + [str(Path(x)) for x in picked if str(Path(x)) not in cur])


def browse_music(key):
    picked = native_dialog("Select music or an audio file",
                           [("Audio / video", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.mp4"), ("All files", "*.*")])
    if picked:
        st.session_state[key] = picked.splitlines()[0]


def recent_videos(limit=12):
    """Videos from every outputs* folder (Digen, Vids, cleaned, edited), newest first."""
    found = []
    for folder in BASE.glob("outputs*"):
        if not folder.is_dir():
            continue
        for p in folder.rglob("*"):
            if (p.suffix.lower() in VIDEO_EXTS and not p.stem.endswith("_web")
                    and TMPDIR not in p.parents):
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    pass
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found[:limit]]


def add_clips(paths):
    """Append videos to the end of the timeline. Returns (added, errors)."""
    added, errors = 0, []
    clips = list(st.session_state.ed_clips)
    for p in paths:
        info = info_for(p)
        if not info:
            errors.append(f"cannot read {p}")
            continue
        clips.append({"id": "p" + uuid.uuid4().hex[:8], "path": info["path"], "name": info["name"],
                      "start": 0.0, "end": round(info["dur"], 2), "volume": 100,
                      "dur": info["dur"], "audio": info["audio"]})
        added += 1
    if added:
        st.session_state.ed_clips = clips
        st.session_state.ed_ver += 1
    return added, errors


# ---------------------------------------------------------------- 1. add videos
with st.container(border=True):
    ui.card_title("1", "Add videos",
                  "Drop files here, paste paths, or use Browse. Videos from your other tools are already "
                  "in the media bin below.")
    up = st.file_uploader("Drop videos here", type=[e.lstrip(".") for e in VIDEO_EXTS],
                          accept_multiple_files=True, key=f"ed_up{st.session_state.ed_up_n}",
                          label_visibility="collapsed")
    if up:
        saved = []
        for f in up:
            sig = (f.name, f.size)
            if sig in st.session_state.ed_seen_uploads:
                continue
            dest = UPLOADS / f"{time.strftime('%H%M%S')}_{Path(f.name).name}"
            dest.write_bytes(f.getbuffer())
            st.session_state.ed_seen_uploads.add(sig)
            saved.append(str(dest))
        n, errs = add_clips(saved)
        for e in errs:
            st.error(e)
        st.session_state.ed_up_n += 1       # clears the uploader
        if n:
            st.rerun()

    c1, c2, c3 = st.columns([5, 1, 1], vertical_alignment="bottom")
    c1.text_area("Video paths", key="ed_paths", height=80, label_visibility="collapsed",
                 placeholder="Paste video paths here, one per line (they are read from where they are)")
    c2.button("Browse...", key="ed_browse", on_click=browse_videos, args=("ed_paths",), use_container_width=True)
    if c3.button("Add", use_container_width=True):
        paths, bad = [], []
        for line in st.session_state.get("ed_paths", "").splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            q = Path(line).expanduser()
            if q.is_file() and q.suffix.lower() in VIDEO_EXTS:
                paths.append(str(q.resolve()))
            else:
                bad.append(line)
        if bad:
            st.warning("Not a video file: " + "; ".join(bad))
        n, errs = add_clips(list(dict.fromkeys(paths)))
        for e in errs:
            st.error(e)
        if n:
            st.session_state.ed_paths = ""
            st.rerun()

# ---------------------------------------------------------------- 2. timeline
with st.container(border=True):
    ui.card_title("2", "Timeline", "Drag scenes to reorder. Click a scene to cut it (Start / End), set its "
                                   "volume, split it, duplicate it or delete it.")
    bin_items = [i for i in (info_for(p) for p in recent_videos()) if i]
    comp_clips = []
    for c in st.session_state.ed_clips:
        i = info_for(c["path"])
        if i:
            comp_clips.append(dict(c, thumb=i["thumb"]))
    val = timeline(clips=comp_clips, bin=bin_items, version=st.session_state.ed_ver,
                   key="ed_timeline", default=None)
    selected_id = None
    if val and isinstance(val, dict) and "clips" in val:
        st.session_state.ed_clips = val["clips"]
        selected_id = val.get("selected")

    clips, clip_error = [], None
    for c in st.session_state.ed_clips:
        dur = float(c["dur"])
        end = float(c["end"]) if c["end"] and c["end"] > 0 else dur
        end = min(end, dur)
        start = float(c["start"] or 0)
        if end <= start:
            clip_error = f"{c['name']}: End must be after Start."
        clips.append({"path": c["path"], "start": start, "end": end, "volume": float(c["volume"]) / 100,
                      "has_audio": bool(c["audio"]), "name": c["name"], "id": c["id"]})
    total = sum(c["end"] - c["start"] for c in clips)
    if clip_error:
        st.error(clip_error)

    pv, opt = st.columns([1, 1], gap="large")
    with pv:
        chosen = next((c for c in clips if c["id"] == selected_id), None)
        if chosen:
            st.caption(f"Preview of the selected scene (only its cut part): {chosen['name']}")
            try:
                st.video(chosen["path"], start_time=int(chosen["start"]), end_time=int(chosen["end"] + 0.99))
            except TypeError:
                st.video(chosen["path"], start_time=int(chosen["start"]))
        else:
            st.caption("Click a scene to preview it here.")
    with opt:
        size_name = st.selectbox("Output size", list(SIZES), key="ed_size",
                                 help="Clips with a different shape get black bars instead of being stretched.")
        fps = st.selectbox("Frame rate", [24, 25, 30, 60], index=2, key="ed_fps")
        if clips:
            st.markdown(f'<div class="facts"><div class="fact"><b>{len(clips)}</b><span>scenes</span></div>'
                        f'<div class="fact"><b>{int(total // 60)}:{int(total % 60):02d}</b><span>total length</span></div></div>',
                        unsafe_allow_html=True)
        if st.button("Clear timeline", disabled=not clips):
            st.session_state.ed_clips = []
            st.session_state.ed_ver += 1
            st.rerun()

    mb1, mb2, mb3 = st.columns([1.2, 1.2, 1.8], vertical_alignment="center")
    merge_clicked = mb1.button("🔗 Merge & preview", use_container_width=True,
                               disabled=not clips or bool(clip_error),
                               help="Joins all scenes in order into one quick, low-quality preview you can play here.")
    export_top = mb2.button("⬇ Export final video", use_container_width=True, type="primary",
                            disabled=not clips or bool(clip_error),
                            help="Full quality, with your sound and voice settings below.")
    with_sound = mb3.checkbox("Include sound settings and voices in the preview", value=False, key="ed_prev_sound")
    preview_slot = st.container()

# ---------------------------------------------------------------- 3. sound
with st.container(border=True):
    ui.card_title("3", "Sound", "Applied to the merged video. Per-scene volume is set on the timeline.")
    a1, a2 = st.columns(2)
    orig_vol = a1.slider("Original sound volume %", 0, 200, 100, key="ed_orig")
    master_vol = a2.slider("Master volume %", 0, 200, 100, key="ed_master")
    mute_orig = st.checkbox("Mute the original sound", key="ed_mute")
    pitch = st.slider("Change the pitch of the original voices (semitones)", -12, 12, 0, key="ed_pitch",
                      help="Simple voice changer: negative = deeper, positive = higher. The speed stays the same.")
    m1, m2 = st.columns([4, 1], vertical_alignment="bottom")
    m1.text_input("Background music (optional)", key="ed_music",
                  placeholder="C:\\Users\\you\\Music\\track.mp3")
    m2.button("Browse...", key="ed_music_btn", on_click=browse_music, args=("ed_music",), use_container_width=True)
    music_vol = st.slider("Music volume %", 0, 100, 25, key="ed_music_vol")

# ---------------------------------------------------------------- 4. voices
with st.container(border=True):
    ui.card_title("4", "Voices", "Write lines of speech. Each line can have its own voice, start time and speed.")
    voices = voice_list()
    locales = sorted({v["Locale"] for v in voices})
    default_loc = locales.index("en-IN") if "en-IN" in locales else (locales.index("en-US") if "en-US" in locales else 0)
    v1, v2 = st.columns([1, 1])
    locale = v1.selectbox("Language / accent", locales, index=default_loc, key="ed_locale")
    pool = [v for v in voices if v["Locale"] == locale]
    names = [v["ShortName"] for v in pool]
    gender = {v["ShortName"]: v["Gender"] for v in pool}
    label = lambda n: f"{n} ({gender.get(n, '')})"
    voice_vol = v2.slider("Voice volume %", 0, 300, 100, key="ed_voice_vol")
    duck = st.checkbox("Lower the original sound while a voice speaks", value=True, key="ed_duck")

    lines = []
    for i in range(st.session_state.ed_nlines):
        st.markdown(f"**Line {i + 1}**")
        t_col, o_col = st.columns([3, 2])
        text = t_col.text_area("Text", key=f"ed_ltext_{i}", height=90, label_visibility="collapsed",
                               placeholder="What the voice should say")
        with o_col:
            voice = st.selectbox("Voice", names, key=f"ed_lvoice_{i}", format_func=label)
            b1, b2, b3 = st.columns(3)
            start = b1.number_input("Starts at (s)", min_value=0.0, value=0.0, step=0.5, key=f"ed_lstart_{i}")
            speed = b2.number_input("Speed %", min_value=-50, max_value=100, value=0, step=5, key=f"ed_lspeed_{i}",
                                    help="0 = normal, -20 = slower, +20 = faster")
            vpitch = b3.number_input("Pitch Hz", min_value=-50, max_value=50, value=0, step=5, key=f"ed_lpitch_{i}",
                                     help="0 = normal, negative = deeper")
        if text.strip() and voice:
            lines.append({"text": text.strip(), "voice": voice, "start": float(start),
                          "speed": int(speed), "pitch": int(vpitch)})
    bb1, bb2, _ = st.columns([1, 1, 4])
    if bb1.button("+ Add line"):
        st.session_state.ed_nlines += 1
        st.rerun()
    if bb2.button("Remove last", disabled=st.session_state.ed_nlines <= 1):
        st.session_state.ed_nlines -= 1
        st.rerun()

    with st.expander("Try a voice"):
        tv, tt = st.columns([2, 3])
        test_voice = tv.selectbox("Voice to test", names, key="ed_test_voice", format_func=label)
        test_text = tt.text_input("Sample text", value="Hello, this is how I sound.", key="ed_test_text")
        if st.button("Play sample"):
            try:
                with st.spinner("Making the sample…"):
                    sample = et.synth(test_text, test_voice, TMPDIR / "sample.mp3")
                st.audio(sample.read_bytes(), format="audio/mp3")
            except et.EditError as e:
                st.error(str(e))

# ---------------------------------------------------------------- build
sound_changed = (orig_vol != 100 or master_vol != 100 or mute_orig or pitch != 0
                 or bool(lines) or bool(st.session_state.get("ed_music", "").strip()))


def timeline_signature():
    return repr([(c["path"], c["start"], c["end"], round(c["volume"], 2)) for c in clips]) + str(size_name)


def make_video(final, quick, use_sound, status):
    """Merge the scenes (and optionally mix sound/voices). quick = small, fast preview."""
    log, warnings = [], []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    size = SIZES[size_name]
    if size is None:
        first = et.probe(clips[0]["path"])
        size = (first["width"] // 2 * 2, first["height"] // 2 * 2)       # even numbers for H.264
    out_fps, preset, crf = fps, "veryfast", 20
    if quick:
        k = min(1.0, 640 / max(size))
        size = (max(2, int(size[0] * k) // 2 * 2), max(2, int(size[1] * k) // 2 * 2))
        out_fps, preset, crf = 24, "ultrafast", 30
    music_path = st.session_state.get("ed_music", "").strip().strip('"')
    if use_sound and music_path and not Path(music_path).is_file():
        raise et.EditError(f"music file not found: {music_path}")

    status.write("Merging and cutting the scenes")
    tl = TMPDIR / f"{stamp}_timeline.mp4"
    et.build_timeline(clips, tl, size, out_fps, log, preset=preset, crf=crf)

    if not (use_sound and sound_changed):
        tl.replace(final)
    else:
        voice_files = []
        for k_, ln in enumerate(lines, 1):
            status.write(f"Making voice {k_}/{len(lines)} ({ln['voice']})")
            mp3 = TMPDIR / f"{stamp}_voice{k_}.mp3"
            et.synth(ln["text"], ln["voice"], mp3, ln["speed"], ln["pitch"])
            voice_files.append((mp3, ln["start"]))
            end_at = ln["start"] + et.probe(mp3, require_video=False)["duration"]
            if end_at > total + 0.2:
                warnings.append(f"Line {k_} ends at {end_at:.1f} s, after the video ends ({total:.1f} s), "
                                f"so its end is cut off. Make the video longer or start the line earlier.")
        status.write("Mixing the sound")
        et.mix_sound(tl, final, log, orig_vol=orig_vol / 100, mute_orig=mute_orig,
                     pitch_semitones=pitch, voices=voice_files, voice_vol=voice_vol / 100,
                     music=music_path or None, music_vol=music_vol / 100,
                     master_vol=master_vol / 100, duck=duck)
    return {"file": str(final), "warnings": warnings, "log": "\n".join(log)}


go_bottom = st.button("✂️  Export final video", type="primary", use_container_width=True,
                      disabled=not clips or bool(clip_error))

# quick preview (shown inside the Timeline card)
if merge_clicked:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with preview_slot:
        try:
            with st.status("Merging a quick preview…", expanded=True) as status:
                res_p = make_video(TMPDIR / f"preview_{stamp}.mp4", True, with_sound, status)
                status.update(label="Preview ready", state="complete")
            res_p["sig"] = timeline_signature()
            st.session_state.ed_preview = res_p
        except et.EditError as e:
            st.session_state.ed_preview = {"error": str(e), "sig": timeline_signature()}
        except Exception as e:
            st.session_state.ed_preview = {"error": f"{type(e).__name__}: {e}", "sig": timeline_signature()}

pr = st.session_state.get("ed_preview")
if pr:
    with preview_slot:
        if pr.get("file") and Path(pr["file"]).exists():
            if pr.get("sig") != timeline_signature():
                st.info("The timeline changed since this preview. Press Merge & preview again to refresh it.")
            st.caption("Preview of all scenes joined in order (small and low quality, just for checking).")
            st.video(pr["file"])
            for w in pr.get("warnings", []):
                st.warning(w)
        elif pr.get("error"):
            st.error(pr["error"])
        if pr.get("log") and pr.get("error"):
            with st.expander("ffmpeg log"):
                st.code(pr["log"], language=None, wrap_lines=True)

# final export
if go_bottom or export_top:
    final = OUTDIR / f"edit_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    try:
        with st.status("Building the final video…", expanded=True) as status:
            st.session_state.ed_result = make_video(final, False, True, status)
            status.update(label="Done", state="complete")
    except et.EditError as e:
        st.session_state.ed_result = {"error": str(e), "log": ""}
    except Exception as e:
        st.session_state.ed_result = {"error": f"{type(e).__name__}: {e}", "log": ""}

# ---------------------------------------------------------------- result
res = st.session_state.get("ed_result")
if res:
    if res.get("file") and Path(res["file"]).exists():
        f = Path(res["file"])
        with st.container(border=True):
            st.markdown('<span class="chip ok">Done</span>', unsafe_allow_html=True)
            ui.card_title("🎞", "Your edited video")
            v_col, i_col = st.columns([1, 1], gap="large")
            with v_col:
                ui.show_video(f)
            with i_col:
                st.caption(f"{f.name}  ·  {f.stat().st_size / 1024 / 1024:.1f} MB")
                st.download_button(f"Download {f.name}", f.read_bytes(), file_name=f.name, key="ed_dl")
                for w in res.get("warnings", []):
                    st.warning(w)
    else:
        st.markdown('<span class="chip bad">Failed</span>', unsafe_allow_html=True)
        st.error(res.get("error", "something went wrong"))
    if res.get("log"):
        with st.expander("ffmpeg log", expanded=bool(res.get("error"))):
            st.code(res["log"], language=None, wrap_lines=True)