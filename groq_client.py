"""Small Groq client (OpenAI-compatible API) and the prompt-writing instructions.

No third-party packages needed. Used by prompt_page.py; the key is passed in by the page.
"""
import json
import os
import re
import urllib.error
import urllib.request

BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

TOOL_HINTS = {
    "Any tool": "Write for a general text-to-video model.",
    "Digen AI": ("Target: Digen's Movie Maker agent. It makes cinematic clips; strong, clear action and camera "
                 "language work best."),
    "Google Vids": ("Target: Google Vids (Veo). Text-to-video, one continuous clip. One clear action per beat, "
                    "no spoken dialogue, no on-screen text."),
    "TAAFT Video": ("Target: the TAAFT video generator (Fast or Quality model). One continuous shot. If a "
                    "reference image is attached, call it <Image 1> and say how it appears in the shot."),
}

SYSTEM = """You are a senior prompt writer for AI video generators.
You turn a short idea into a sequence of scene prompts. Return ONLY a JSON object, no markdown:
{"title": "short project title", "scenes": [{"title": "short scene title", "prompt": "the prompt"}]}

Rules for every prompt:
- It describes ONE continuous shot (no cuts) that lasts exactly the requested clip length.
- Pace the action with timed beats inside the prose, for example "0-3 s: ...; 3-7 s: ...; 7-10 s: ..." and
  make the last beat end exactly at the clip length.
- Cover subject, action, camera movement, lighting, colour palette, setting and mood. Add a few words about
  ambient sound only when it helps.
- Do not mention aspect ratio, resolution or the total video length; the tool adds those.
- Keep characters, products, wardrobe and setting identical across scenes: repeat the key identifying details
  in every scene prompt. Scene N must start where scene N-1 ended.
- 60 to 110 words per prompt. Plain, concrete, visual language. No camera brand names, no hashtags.
- No on-screen text unless the user asks for it; if they do, give the exact short text in quotes.
- No real people, no copyrighted characters, nothing violent or unsafe.
- Write the prompts in the requested language."""


def _request(method, path, key, body=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "video-studio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        hint = {401: " (the API key was rejected)", 429: " (rate limit: wait a moment and retry)"}.get(e.code, "")
        raise RuntimeError(f"Groq error {e.code}{hint}: {str(detail)[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach Groq: {e.reason}") from None


def list_models(key):
    """Model ids the key can use (also proves that the key works)."""
    return sorted(m["id"] for m in _request("GET", "/models", key, timeout=30).get("data", []))


def chat_json(key, model, messages, temperature=0.8, effort="low", max_tokens=6000):
    """Chat completion that should answer with a JSON object. Returns the message text."""
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_completion_tokens": max_tokens, "reasoning_effort": effort,
            "response_format": {"type": "json_object"}}
    try:
        data = _request("POST", "/chat/completions", key, body)
    except RuntimeError as e:
        if "response_format" not in str(e):
            raise
        body.pop("response_format")
        data = _request("POST", "/chat/completions", key, body)
    return data["choices"][0]["message"].get("content") or ""


def build_messages(idea, n_scenes, clip_s, orientation, tool, style, has_image, language):
    user = (f"Idea / brief:\n{idea.strip()}\n\n"
            f"Number of scenes: {n_scenes}\n"
            f"Clip length per scene: exactly {clip_s} seconds\n"
            f"Orientation: {orientation} (compose the shot for it)\n"
            f"Language of the prompts: {language}\n"
            f"{TOOL_HINTS.get(tool, TOOL_HINTS['Any tool'])}\n")
    if style.strip():
        user += f"Style / brand notes: {style.strip()}\n"
    if has_image:
        user += "A reference image of the main subject will be attached to the generator.\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def refine_messages(base, scenes, instruction):
    cur = json.dumps({"scenes": [{"title": s["title"], "prompt": s["prompt"]} for s in scenes]}, ensure_ascii=False)
    return base + [
        {"role": "assistant", "content": cur},
        {"role": "user", "content": f"Change the scenes as follows: {instruction.strip()}\n"
                                    f"Keep every rule, the clip length and the same number of scenes. "
                                    f"Return the full JSON again."},
    ]


def parse_scenes(text):
    """(title, [{'title','prompt'}, ...]) from the model's JSON answer; raises ValueError if unusable."""
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            raise ValueError("the answer was not JSON")
        data = json.loads(m.group(0))
    scenes = data.get("scenes") if isinstance(data, dict) else data
    out = []
    for s in scenes or []:
        if isinstance(s, dict) and str(s.get("prompt", "")).strip():
            out.append({"title": str(s.get("title", "")).strip(), "prompt": str(s["prompt"]).strip()})
    if not out:
        raise ValueError("the answer had no scene prompts")
    return (str(data.get("title", "")).strip() if isinstance(data, dict) else ""), out