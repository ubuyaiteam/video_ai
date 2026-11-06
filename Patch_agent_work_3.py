"""Run once next to agent_work.py (after patch_agent_work_2.py):   python patch_agent_work_3.py
As soon as the page shows "Digen will continue working after your reply" (and it stays for
REPLY_WAIT_S seconds), the worker sends "continue" to the chat. It waits REPLY_COOLDOWN_S
between replies and sends at most MAX_REPLIES per run, so it cannot spam the agent.
Backup: agent_work.py.bak3
"""
import shutil
import sys
from pathlib import Path

P = Path("agent_work.py")
if not P.exists():
    sys.exit("agent_work.py not found in this folder")
shutil.copy(P, "agent_work.py.bak3")
s = P.read_text(encoding="utf-8")


def swap(old, new, what):
    global s
    if old not in s:
        sys.exit(f"anchor not found for: {what}  (nothing was changed; did you run patch 2 first?)")
    s = s.replace(old, new, 1)


swap("STALL_S = 600",
     "REPLY_TEXT = \"continue\"   # what is sent when the agent waits for a reply\n"
     "REPLY_WAIT_S = 20        # the notice must stay this long before acting\n"
     "REPLY_COOLDOWN_S = 180   # minimum time between two replies\n"
     "MAX_REPLIES = 5\n"
     "STALL_S = 600", "constants")

swap('        stall = {"sig": None',
     '        reply_seen = {"since": None, "last": 0.0, "n": 0}\n'
     '        stall = {"sig": None', "init")

swap("            sig = (pcts, waiting_reply, len(vids))\n",
     "            # ---- the agent says it continues after our reply -> reply\n"
     "            if waiting_reply and not done_at:\n"
     "                now_t = time.time()\n"
     "                if reply_seen[\"since\"] is None:\n"
     "                    reply_seen[\"since\"] = now_t\n"
     "                    log(\"notice seen: 'Digen will continue working after your reply'\")\n"
     "                elif (now_t - reply_seen[\"since\"] > REPLY_WAIT_S\n"
     "                      and now_t - reply_seen[\"last\"] > REPLY_COOLDOWN_S\n"
     "                      and reply_seen[\"n\"] < MAX_REPLIES):\n"
     "                    try:\n"
     "                        send_reply(page, REPLY_TEXT)\n"
     "                        reply_seen[\"n\"] += 1\n"
     "                        log(f\"replied {reply_seen['n']}/{MAX_REPLIES}\")\n"
     "                    except Exception as e:\n"
     "                        log(f\"reply failed: {str(e).splitlines()[0]}\", \"WARN\")\n"
     "                    reply_seen[\"last\"] = time.time()\n"
     "                    reply_seen[\"since\"] = None\n"
     "            else:\n"
     "                reply_seen[\"since\"] = None\n"
     "            sig = (pcts, waiting_reply, len(vids))\n", "reply block")

P.write_text(s, encoding="utf-8")
print("agent_work.py patched (backup: agent_work.py.bak3)")