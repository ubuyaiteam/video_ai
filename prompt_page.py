"""AI prompt desk: writes scene prompts with a Groq model (gpt-oss) and sends any scene straight to the
Google Vids, Digen AI or TAAFT page.

Page of the combined app: run `streamlit run streamlit_app.py`.
Needs groq_client.py and ui_common.py next to it. The Groq key is typed in the sidebar (or set as the
GROQ_API_KEY environment variable); it is kept only in this browser session unless you tick "Remember".
"""
import importlib
import json
import os
from pathlib import Path

import streamlit as st

import ui_common
importlib.reload(ui_common)
from ui_common import card_title, hero, inject_css

import groq_client as gc
importlib.reload(gc)

BASE = Path(__file__).parent.resolve()
KEY_FILE = BASE / ".groq_key"
TOOLS = {                                  # label -> (page file, session key of that page's prompt box)
    "Google Vids": ("vids_page.py", "shot_prompt_vids"),
    "Digen AI": ("digen_page.py", "shot_prompt_digen"),
    "TAAFT Video": ("taaft_page.py", "shot_prompt_taaft"),
}
LANGS = ["English", "Hindi", "Spanish", "French", "German", "Arabic", "Portuguese"]

inject_css()

# ---------------------------------------------------------------- state
if "groq_key" not in st.session_state:
    saved = KEY_FILE.read_text(encoding="utf-8").strip() if KEY_FILE.exists() else ""
    st.session_state["groq_key"] = os.environ.get("GROQ_API_KEY", "") or saved
st.session_state.setdefault("gen", 0)
st.session_state.setdefault("scenes", [])
st.session_state.setdefault("project_title", "")
st.session_state.setdefault("base_messages", None)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Groq connection")
    key = st.text_input("Groq API key", key="groq_key", type="password", placeholder="gsk_...",
                        help="Create a key in the Groq console. It is sent only to Groq's API from this computer.")
    model = st.selectbox("Model", gc.MODELS, help="gpt-oss-120b writes better scenes; gpt-oss-20b is faster.")
    effort = st.radio("Thinking effort", ["low", "medium", "high"], horizontal=True,
                      help="Higher effort is slower but plans the scenes more carefully.")
    temperature = st.slider("Creativity", 0.0, 1.5, 0.8, 0.1)
    remember = st.checkbox("Remember the key on this computer", value=KEY_FILE.exists(),
                           help="Saves the key as plain text in a hidden file next to the app. Leave off on shared PCs.")
    if remember and key and not (KEY_FILE.exists() and KEY_FILE.read_text(encoding="utf-8").strip() == key):
        KEY_FILE.write_text(key, encoding="utf-8")
    if not remember and KEY_FILE.exists():
        KEY_FILE.unlink(missing_ok=True)
    if st.button("Test connection", disabled=not key):
        try:
            ids = gc.list_models(key)
            st.success(f"Connected. {len(ids)} models available.")
        except RuntimeError as e:
            st.error(str(e))

# ---------------------------------------------------------------- hero
hero("🧠 Groq · gpt-oss", "Prompt desk",
     "Describe your idea once. The AI writes one prompt per scene, sized for a single clip, and you send "
     "any scene straight to Google Vids, Digen AI or TAAFT.")

left, right = st.columns([1.1, 1], gap="large")
with left:
    with st.container(border=True):
        card_title("1", "Your idea", "Product, story, mood, anything the video should show.")
        idea = st.text_area("Idea", key="idea", height=140, label_visibility="collapsed",
                            placeholder="A Halloween sale video for Ubuy: a friendly ambassador walks through a "
                                        "glowing pumpkin-lit warehouse and shows her phone with the deals")
        style = st.text_input("Style or brand notes (optional)", key="style",
                              placeholder="warm orange and purple light, cinematic, smooth camera, Ubuy brand feel")

with right:
    with st.container(border=True):
        card_title("2", "Scenes and tool")
        c1, c2 = st.columns(2)
        n_scenes = c1.slider("Scenes", 1, 12, 3)
        clip_s = c2.number_input("Seconds per scene", min_value=4, max_value=15, value=10, step=1,
                                 help="Each prompt is paced to exactly this length. Vids and TAAFT usually "
                                      "return about 8 s clips; set 8 if you want the pacing to match them.")
        orientation = st.radio("Orientation", ["portrait 9:16", "landscape 16:9"], horizontal=True)
        tool = st.selectbox("Write for", ["Any tool"] + list(TOOLS), help="Adds a short style hint for that tool.")
        c3, c4 = st.columns(2)
        language = c3.selectbox("Prompt language", LANGS)
        has_image = c4.checkbox("I will attach a reference image", value=False)

go = st.button("🧠  Write the scene prompts", type="primary", use_container_width=True,
               disabled=not (idea.strip() and key))
if not key:
    st.caption("Add your Groq API key in the sidebar to start.")

if go:
    base = gc.build_messages(idea, n_scenes, int(clip_s), orientation, tool, style, has_image, language)
    with st.spinner(f"{model} is writing {n_scenes} scene prompt(s)..."):
        try:
            text = gc.chat_json(key, model, base, temperature, effort)
            title, scenes = gc.parse_scenes(text)
            st.session_state.update(scenes=scenes, project_title=title, base_messages=base,
                                    gen=st.session_state["gen"] + 1)
        except (RuntimeError, ValueError) as e:
            st.error(f"Could not get scene prompts: {e}")

# ---------------------------------------------------------------- scenes
scenes = st.session_state["scenes"]
gen = st.session_state["gen"]
if scenes:
    st.divider()
    st.subheader(st.session_state["project_title"] or "Scene prompts")
    st.caption(f"{len(scenes)} scene(s), each paced for one {int(clip_s)} s clip. Edit any text, then send the "
               f"scene to a tool. The tool page adds aspect ratio and length itself.")

    for i, sc in enumerate(scenes):
        with st.container(border=True):
            st.markdown(f'<div class="card-title"><span class="dot">{i + 1}</span>'
                        f'{sc["title"] or "Scene " + str(i + 1)}</div>', unsafe_allow_html=True)
            txt = st.text_area(f"Prompt {i + 1}", value=sc["prompt"], key=f"sp_{gen}_{i}", height=150,
                               label_visibility="collapsed")
            scenes[i]["prompt"] = txt
            st.caption(f"{len(txt.split())} words")
            cols = st.columns(len(TOOLS))
            for col, (label, (page, pkey)) in zip(cols, TOOLS.items()):
                if col.button(f"Send to {label}", key=f"send_{gen}_{i}_{label}", use_container_width=True):
                    st.session_state[pkey] = txt
                    st.switch_page(page)

    with st.container(border=True):
        card_title("✎", "Ask the AI to change something", "It rewrites all scenes and keeps them consistent.")
        instr = st.text_input("Change request", key="refine_text", label_visibility="collapsed",
                              placeholder="make it more playful, add a close-up of the phone in scene 2 ...")
        c5, c6 = st.columns([1, 1])
        if c5.button("Rewrite the scenes", disabled=not (instr.strip() and key and st.session_state["base_messages"])):
            with st.spinner("Rewriting..."):
                try:
                    msgs = gc.refine_messages(st.session_state["base_messages"], scenes, instr)
                    title, new = gc.parse_scenes(gc.chat_json(key, model, msgs, temperature, effort))
                    st.session_state.update(scenes=new, project_title=title or st.session_state["project_title"],
                                            gen=gen + 1)
                    st.rerun()
                except (RuntimeError, ValueError) as e:
                    st.error(f"Could not rewrite: {e}")
        allt = "\n\n".join(f"Scene {i + 1}: {s['title']}\n{s['prompt']}" for i, s in enumerate(scenes))
        c6.download_button("Download all prompts (.txt)", allt, file_name="scene_prompts.txt")