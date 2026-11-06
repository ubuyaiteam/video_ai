"""Run once next to agent_work.py (after patch_agent_work.py):   python patch_agent_work_2.py
Fixes runs that wait forever with "distinct videos=0" while the site shows a progress ring
stuck at e.g. 93% or the notice "Digen will continue working after your reply".

  * progress rings (93%) now count as "agent busy"
  * if nothing changes for 10 min: reload the page (free), then send "continue" if the
    agent is waiting for a reply; up to 4 actions per run, 10 min apart
Backup: agent_work.py.bak2
"""
import shutil
import sys
from pathlib import Path

P = Path("agent_work.py")
if not P.exists():
    sys.exit("agent_work.py not found in this folder")
shutil.copy(P, "agent_work.py.bak2")
s = P.read_text(encoding="utf-8")


def swap(old, new, what):
    global s
    if old not in s:
        sys.exit(f"anchor not found for: {what}  (nothing was changed)")
    s = s.replace(old, new, 1)


swap('BUSY_RE = re.compile(r"Digen is thinking|Generating (?:video|image)|Thinking \\d+:\\d{2}", re.I)\n',
     'BUSY_RE = re.compile(r"Digen is thinking|Generating (?:video|image)|Thinking \\d+:\\d{2}", re.I)\n'
     '\n'
     '# Progress rings such as "93%" (ignores the zoom label "100%" and the "50% OFF" badge).\n'
     'PCT_RE = re.compile(r"(?<![\\d.])(\\d{1,2})%(?!\\s*OFF)", re.I)\n'
     'WAIT_REPLY_RE = re.compile(r"will continue working after your reply", re.I)\n'
     'STALL_S = 600            # nothing changed for this long -> reload / nudge\n'
     'MAX_STALL_ACTIONS = 4\n', "BUSY_RE")

swap("def dismiss_popups(page):",
     'def send_reply(page, text):\n'
     '    """Type a short reply in the chat box and send it."""\n'
     '    box = page.locator(PROMPT_SELECTOR).first\n'
     '    box.click(timeout=5000)\n'
     '    page.keyboard.press("Control+A")\n'
     '    page.keyboard.press("Delete")\n'
     '    page.keyboard.insert_text(text)\n'
     '    page.wait_for_timeout(500)\n'
     '    pos = page.evaluate(SEND_POS_JS)\n'
     '    if pos and not pos["disabled"]:\n'
     '        page.mouse.click(pos["x"], pos["y"])\n'
     '    else:\n'
     '        page.keyboard.press("Enter")\n'
     '    log(f"sent reply: {text}")\n'
     '\n\n'
     "def dismiss_popups(page):", "send_reply helper")

swap('        last_text, last_shot, last_print = "", start, 0\n',
     '        last_text, last_shot, last_print = "", start, 0\n'
     '        stall = {"sig": None, "since": time.time(), "actions": 0, "nvids": 0}\n', "stall init")

swap("            if BUSY_RE.search(body):\n                idle_since = None\n",
     "            pcts = tuple(sorted(int(x) for x in PCT_RE.findall(body)))\n"
     "            if BUSY_RE.search(body) or pcts:\n                idle_since = None\n", "busy check")

swap("            t = time.time()\n            if vids and ((done_at",
     "            # ---- stall handling: same progress and no new video for STALL_S\n"
     "            waiting_reply = bool(WAIT_REPLY_RE.search(body))\n"
     "            sig = (pcts, waiting_reply, len(vids))\n"
     "            if sig != stall[\"sig\"]:\n"
     "                stall[\"sig\"], stall[\"since\"] = sig, time.time()\n"
     "                if pcts:\n"
     "                    log(f\"progress: {list(pcts)}%\")\n"
     "            elif (not vids and not done_at and stall[\"actions\"] < MAX_STALL_ACTIONS\n"
     "                  and time.time() - stall[\"since\"] > STALL_S):\n"
     "                stall[\"actions\"] += 1\n"
     "                stall[\"since\"] = time.time()\n"
     "                try:\n"
     "                    if waiting_reply and stall[\"actions\"] % 2 == 0:\n"
     "                        log(\"agent waits for a reply; sending 'continue'\", \"WARN\")\n"
     "                        send_reply(page, \"continue\")\n"
     "                    else:\n"
     "                        log(f\"no change for {STALL_S // 60} min (progress {list(pcts)}%); reloading the page\", \"WARN\")\n"
     "                        page.reload(wait_until=\"domcontentloaded\")\n"
     "                        page.wait_for_timeout(8000)\n"
     "                        dismiss_popups(page)\n"
     "                except Exception as e:\n"
     "                    log(f\"stall action failed: {str(e).splitlines()[0]}\", \"WARN\")\n"
     "\n"
     "            t = time.time()\n            if vids and ((done_at", "stall block")

P.write_text(s, encoding="utf-8")
print("agent_work.py patched (backup: agent_work.py.bak2)")