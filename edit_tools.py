"""Video editing helpers for the Edit page: probe, merge/cut, sound mix, text-to-speech voices.

Needs ffmpeg + ffprobe on PATH, and `pip install edge-tts` for the voices (free, needs internet).
No Streamlit code in here, so it can also be used from a script.
"""
import asyncio
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class EditError(RuntimeError):
    pass


def _candidate_dirs():
    """Places ffmpeg is usually installed on Windows, in case PATH of this process does not have it."""
    dirs = []
    if os.environ.get("FFMPEG_DIR"):
        dirs.append(Path(os.environ["FFMPEG_DIR"]))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
        pk = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if pk.is_dir():
            for exe in pk.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
                dirs.append(exe.parent)
    for base in (os.environ.get("ProgramFiles"), os.environ.get("SystemDrive", "C:") + "\\"):
        if base:
            dirs += [Path(base) / "ffmpeg" / "bin", Path(base) / "ffmpeg"]
    return dirs


def ensure_on_path():
    """If ffmpeg/ffprobe are not found, look in the usual install folders and add them to PATH
    for this process (so every later shutil.which / subprocess call finds them)."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    for d in _candidate_dirs():
        if (d / "ffmpeg.exe").exists() and (d / "ffprobe.exe").exists():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            return True
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


ensure_on_path()


def tools_ok():
    return ensure_on_path()


def run(cmd, log):
    """Run a command, log it and its stderr tail. Returns the CompletedProcess."""
    log.append("$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stderr.strip():
        log.append(r.stderr.strip()[-1800:])
    return r


# ---------------------------------------------------------------- probe
def probe(path, require_video=True):
    """{'duration': s, 'has_audio': bool, 'width': int, 'height': int}
    require_video=False lets audio-only files (voices, music) be measured too."""
    r = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
        capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise EditError(f"cannot read {path}")
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if require_video and not video:
        raise EditError(f"no video stream in {path}")
    return {"duration": float(data.get("format", {}).get("duration") or 0),
            "has_audio": bool(audio),
            "width": int(video[0].get("width") or 0) if video else 0,
            "height": int(video[0].get("height") or 0) if video else 0}


# ---------------------------------------------------------------- merge + cut
def build_timeline(clips, out_path, size, fps, log, preset="veryfast", crf=20):
    """Cut each clip (start..end), set its volume, scale/pad to `size` and join them in order.

    clips: list of dicts {path, start, end, volume (factor, 1.0 = unchanged), has_audio}
    size:  (width, height)
    """
    if not clips:
        raise EditError("no clips selected")
    W, H = size
    cmd = [shutil.which("ffmpeg"), "-y", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", c["path"]]
    parts, labels = [], []
    for i, c in enumerate(clips):
        s, e = float(c["start"]), float(c["end"])
        if e <= s:
            raise EditError(f"clip {i + 1}: end must be after start")
        parts.append(
            f"[{i}:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if c["has_audio"]:
            parts.append(
                f"[{i}:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,aresample=44100,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,volume={float(c['volume']):.3f}[a{i}]")
        else:   # silent clip: add silence of the same length so the join stays in sync
            parts.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={e - s:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append("".join(labels) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    r = run(cmd, log)
    if r.returncode != 0 or not Path(out_path).exists():
        raise EditError("ffmpeg could not build the timeline (see the log)")
    return Path(out_path)


# ---------------------------------------------------------------- voices
FALLBACK_VOICES = [
    {"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"},
    {"ShortName": "en-US-GuyNeural", "Gender": "Male", "Locale": "en-US"},
    {"ShortName": "en-GB-SoniaNeural", "Gender": "Female", "Locale": "en-GB"},
    {"ShortName": "en-GB-RyanNeural", "Gender": "Male", "Locale": "en-GB"},
    {"ShortName": "en-IN-NeerjaNeural", "Gender": "Female", "Locale": "en-IN"},
    {"ShortName": "en-IN-PrabhatNeural", "Gender": "Male", "Locale": "en-IN"},
    {"ShortName": "hi-IN-SwaraNeural", "Gender": "Female", "Locale": "hi-IN"},
    {"ShortName": "hi-IN-MadhurNeural", "Gender": "Male", "Locale": "hi-IN"},
]


def _run_async(coro_fn):
    """Run an async function from any thread (Streamlit's script thread included)."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_fn())).result()


def list_voices():
    """All edge-tts voices as dicts (ShortName, Gender, Locale). Falls back to a short list offline."""
    try:
        import edge_tts

        async def go():
            return await edge_tts.list_voices()
        voices = _run_async(go)
        return [{"ShortName": v["ShortName"], "Gender": v.get("Gender", ""), "Locale": v.get("Locale", "")}
                for v in voices]
    except Exception:
        return FALLBACK_VOICES


def synth(text, voice, out_path, rate_pct=0, pitch_hz=0):
    """Text -> speech mp3 with the chosen voice. rate_pct: -50..+100, pitch_hz: -50..+50."""
    try:
        import edge_tts
    except ImportError:
        raise EditError("the 'edge-tts' package is missing: pip install edge-tts")

    async def go():
        await edge_tts.Communicate(text, voice, rate=f"{int(rate_pct):+d}%",
                                   pitch=f"{int(pitch_hz):+d}Hz").save(str(out_path))
    try:
        _run_async(go)
    except Exception as e:
        raise EditError(f"voice generation failed ({voice}): {e}")
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        raise EditError(f"voice generation produced no audio ({voice})")
    return Path(out_path)


# ---------------------------------------------------------------- sound mix
def mix_sound(video_in, out_path, log, orig_vol=1.0, mute_orig=False, pitch_semitones=0,
              voices=(), voice_vol=1.0, music=None, music_vol=0.3, master_vol=1.0, duck=True):
    """Replace the audio track of video_in with a mix, keeping the picture untouched.

    voices: list of (mp3_path, start_seconds). music: optional audio/video file, looped to the length
    of the video. pitch_semitones changes the pitch of the ORIGINAL audio (a simple voice changer).
    """
    cmd = [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(video_in)]
    for v, _ in voices:
        cmd += ["-i", str(v)]
    music_idx = None
    if music:
        music_idx = 1 + len(voices)
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    def graph(norm):
        f = []
        orig = f"[0:a]volume={0 if mute_orig else orig_vol:.3f}"
        if pitch_semitones:
            fac = 2 ** (pitch_semitones / 12)
            orig += f",aresample=44100,asetrate={round(44100 * fac)},atempo={1 / fac:.5f},aresample=44100"
        f.append(orig + "[o]")

        mix_in = []
        if voices:
            for j, (_, start) in enumerate(voices, 1):
                ms = max(0, int(float(start) * 1000))
                f.append(f"[{j}:a]aresample=44100,aformat=channel_layouts=stereo,"
                         f"adelay={ms}|{ms},volume={voice_vol:.3f}[vo{j}]")
            if len(voices) > 1:
                f.append("".join(f"[vo{j}]" for j in range(1, len(voices) + 1)) +
                         f"amix=inputs={len(voices)}:duration=longest{norm}[vb]")
            else:
                f.append("[vo1]anull[vb]")
            if duck:   # lower the original while a voice speaks
                f.append("[vb]asplit=2[vm][vs]")
                f.append("[o][vs]sidechaincompress=threshold=0.03:ratio=10:attack=20:release=400[od]")
            else:
                f.append("[vb]anull[vm]")
                f.append("[o]anull[od]")
            mix_in = ["[od]", "[vm]"]
        else:
            mix_in = ["[o]"]
        if music_idx is not None:
            f.append(f"[{music_idx}:a]aresample=44100,aformat=channel_layouts=stereo,volume={music_vol:.3f}[mu]")
            mix_in.append("[mu]")
        if len(mix_in) > 1:
            f.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first{norm},volume={master_vol:.3f}[aout]")
        else:
            f.append(f"{mix_in[0]}volume={master_vol:.3f}[aout]")
        return ";".join(f)

    def attempt(norm):
        c = cmd + ["-filter_complex", graph(norm), "-map", "0:v", "-map", "[aout]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
        return run(c, log)

    r = attempt(":normalize=0")          # keeps each track at its own volume (ffmpeg 4.4+)
    if r.returncode != 0 and "normalize" in (r.stderr or "").lower():
        log.append("this ffmpeg has no amix normalize option; retrying without it")
        r = attempt("")
    if r.returncode != 0 or not Path(out_path).exists():
        raise EditError("ffmpeg could not mix the sound (see the log)")
    return Path(out_path)


# ---------------------------------------------------------------- thumbnails
def thumbnail(path, width=240):
    """First-second frame of a video as a base64 JPEG data URI ('' if it cannot be made)."""
    import base64
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return ""
    for seek in ("0.3", "0"):
        r = subprocess.run([ffmpeg, "-loglevel", "error", "-ss", seek, "-i", str(path), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        if r.returncode == 0 and r.stdout:
            return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode()
    return ""