"""Cookie manager for the Google Flow tool.

Used by flow_work.py (load_cookies) and flow_page.py (parse, save, list, delete).
Accepts EditThisCookie / Playwright / {"cookies": [...]} JSON exports.

Export while signed in to Google and include ALL cookies for google.com, not only
flow.google.com: the login lives in SID, __Secure-1PSID, __Secure-3PSID and friends.
"""
import json
import re
from pathlib import Path

AUTH_NAMES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID",
              "__Secure-1PAPISID", "__Secure-3PAPISID"}
SAMESITE = {"no_restriction": "None", "none": "None", "unspecified": "Lax", "lax": "Lax", "strict": "Strict"}


def _as_list(data):
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    return data


def parse_cookies(raw):
    """Validate a cookie export (JSON text). Returns the list, raises ValueError if unusable."""
    data = _as_list(json.loads(raw))
    if not isinstance(data, list) or not data:
        raise ValueError("expected a JSON list of cookies")
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            raise ValueError("each cookie needs a name and a value")
    return data


def has_login(cookies):
    """True if the list contains at least one Google login cookie."""
    return bool({c.get("name") for c in cookies} & AUTH_NAMES)


def safe_name(name):
    stem = re.sub(r"[^\w.-]+", "_", Path(name).stem).strip("._") or "cookies"
    return stem + ".json"


def save_cookie(folder, name, data):
    """Save under folder/<safe name>.json. Returns (path, login_cookies_missing)."""
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    path = folder / safe_name(name)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path, not has_login(data)


def list_cookie_files(folder):
    """{file name: Path} for every .json in the folder."""
    return {p.name: p for p in sorted(Path(folder).glob("*.json"))}


def delete_cookie(path):
    Path(path).unlink(missing_ok=True)


def load_cookies(path):
    """Read a cookie file and return Playwright-ready cookies (sameSite / expiry fixed)."""
    data = _as_list(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    out = []
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        domain = c.get("domain")
        if not domain and not c.get("url"):
            continue
        ck = {"name": c["name"], "value": c["value"], "path": c.get("path") or "/",
              "secure": bool(c.get("secure", False)), "httpOnly": bool(c.get("httpOnly", False))}
        if domain:
            ck["domain"] = domain
        else:
            ck["url"] = c["url"]
        ck["sameSite"] = SAMESITE.get(str(c.get("sameSite", "")).lower(), "Lax")
        if ck["sameSite"] == "None":
            ck["secure"] = True                      # browsers reject SameSite=None without Secure
        exp = c.get("expirationDate", c.get("expires"))
        if isinstance(exp, (int, float)) and exp > 0 and not c.get("session"):
            ck["expires"] = float(exp)
        out.append(ck)
    return out