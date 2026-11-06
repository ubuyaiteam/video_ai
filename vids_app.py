"""Video studio: one Streamlit app with a page per tool.

Run:  streamlit run streamlit_app.py

Pages (each is its own file, so you can edit them separately):
  prompt_page.py ->  AI Prompts (Groq gpt-oss writes scene prompts and sends them to the tools below)
  digen_page.py  ->  Digen AI   (worker: agent_work.py,  outputs in outputs/,      cookies in cookies/)
  vids_page.py   ->  Google Vids (worker: vids_work.py,  outputs in outputs_vids/, cookies in cookies_vids/)
  taaft_page.py  ->  TAAFT Video (worker: taaft_work.py, outputs in outputs_taaft/, cookies in cookies_taaft/, cookies optional)
  upscale_page.py ->  Upscaler   (worker: upscale_work.py, outputs in outputs_upscaled/, no login)
Digen and Vids need cookie_loader.py; the Upscaler page needs ui_common.py and Chrome/Edge. The two pages keep separate jobs, logs, cookie folders and output folders.
"""
import streamlit as st

st.set_page_config(page_title="Video studio", page_icon="🎬", layout="wide")

pages = [
    st.Page("prompt_page.py", title="AI Prompts", icon="🧠", default=True),
    st.Page("digen_page.py", title="Digen AI", icon="🎬"),
    st.Page("vids_page.py", title="Google Vids", icon="📽️"),
    st.Page("flow_page.py", title="Flow Vids", icon="📽️"),
    st.Page("taaft_page.py", title="TAAFT Video(IN TESTING)", icon="✨"),
    st.Page("upscale_page.py", title="Upscaler", icon="🔍"),
    st.Page("wm_page.py", title="wm", icon="🔍"),
    st.Page("edit_page.py", title="edit", icon="✨"),
    st.Page("credit_page.py", title="credit", icon="✨"),
     
]
try:
    nav = st.navigation(pages, position="top")
except TypeError:                      # older Streamlit: navigation goes in the sidebar
    nav = st.navigation(pages)
nav.run()