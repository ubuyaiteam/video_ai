"""Cookie loader shared by agent_work.py (Digen) and vids_work.py (Google Vids).

load_cookies(path) reads a cookie export (EditThisCookie, Cookie-Editor, or Playwright's own
format) and returns a list that Playwright's context.add_cookies() accepts.
"""
import json
from pathlib import Path

_SAMESITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
}


def _convert(c):
    """One exported cookie -> Playwright cookie dict, or None if it is unusable."""
    if not isinstance(c, dict) or not c.get("name") or "value" not in c:
        return None
    out = {"name": str(c["name"]), "value": str(c["value"])}

    domain = c.get("domain")
    path = c.get("path") or "/"
    if out["name"].startswith("__Host-") and domain:
        # __Host- cookies must not carry a Domain attribute
        out["url"] = f"https://{str(domain).lstrip('.')}/"
    elif domain:
        out["domain"] = str(domain)
        out["path"] = path
    elif c.get("url"):
        out["url"] = c["url"]
    else:
        return None

    exp = c.get("expires", c.get("expirationDate"))
    if isinstance(exp, (int, float)) and exp > 0:
        out["expires"] = int(exp)

    if c.get("httpOnly") is not None:
        out["httpOnly"] = bool(c["httpOnly"])
    if c.get("secure") is not None:
        out["secure"] = bool(c["secure"])

    same = _SAMESITE.get(str(c.get("sameSite", "")).strip().lower())
    if same:
        out["sameSite"] = same
        if same == "None":
            out["secure"] = True          # browsers require Secure with SameSite=None
    return out


def load_cookies(path):
    """Return the cookies in `path` as a list ready for context.add_cookies()."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of cookies")
    cookies = [x for x in (_convert(c) for c in data) if x]
    if not cookies:
        raise ValueError(f"{path}: no usable cookies found")
    return cookies