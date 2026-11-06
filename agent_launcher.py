"""Starts agent_work.py without editing it.

Your cookies_util.py has no load_cookies(), which agent_work.py needs. This launcher adds the
function (from cookie_loader.py) to the cookies_util module in memory, then runs agent_work.py
unchanged with the same command-line arguments. Nothing on disk is modified.
"""
import runpy
import sys
import types
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

from cookie_loader import load_cookies  # noqa: E402

try:
    import cookies_util
except Exception:                      # the file is broken or missing: use an empty stand-in
    cookies_util = types.ModuleType("cookies_util")
    sys.modules["cookies_util"] = cookies_util
if not hasattr(cookies_util, "load_cookies"):
    cookies_util.load_cookies = load_cookies

target = HERE / "agent_work.py"
if not target.exists():
    sys.exit(f"agent_work.py not found in {HERE}")
sys.argv[0] = str(target)
runpy.run_path(str(target), run_name="__main__")