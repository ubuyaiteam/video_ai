"""Declares the drag and drop timeline component.

It lives in its own module because Streamlit cannot declare a component from inside a page
script run through st.navigation (it fails with "module is None").
"""
from pathlib import Path

import streamlit.components.v1 as components

_timeline = components.declare_component(
    "vid_timeline", path=str(Path(__file__).parent / "timeline_component"))


def timeline(**kwargs):
    return _timeline(**kwargs)