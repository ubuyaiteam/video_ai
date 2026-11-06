"""Run once in the flow_ai folder:   python fix_agent_import.py
Changes line 16 of agent_work.py to import load_cookies from cookie_loader.py
(backup: agent_work.py.bak4). Your cookies_util.py is not touched.
"""
import shutil
import sys
from pathlib import Path

p = Path("agent_work.py")
if not p.exists():
    sys.exit("agent_work.py not found in this folder")
if not Path("cookie_loader.py").exists():
    sys.exit("cookie_loader.py is missing: copy it into this folder first")
s = p.read_text(encoding="utf-8")
old = "from cookies_util import load_cookies"
if old not in s:
    sys.exit("the import line was not found (already changed?)")
shutil.copy(p, "agent_work.py.bak4")
p.write_text(s.replace(old, "from cookie_loader import load_cookies", 1), encoding="utf-8")
print("done: agent_work.py now uses cookie_loader.py")