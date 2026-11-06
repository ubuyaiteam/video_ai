"""Credit collector for agent.digen.ai.

For every cookie file: open Digen, read the credit balance, look for free-credit buttons
(daily claim / check-in / collect...) on the page and in the credits menu, click them, then read the
balance again and report the difference.

Usage:
    python credit_work.py --cookies cookies/ acc1.json --outdir outputs_credits [--headed] [--no-claim]

Prints one JSON line at the end: {"ok": true, "accounts": [...], "gained": N, ...}
Each run also appends one row per account to <outdir>/history.csv.
"""
import argparse
import csv
import json
import platform
import re
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

AGENT_URL = "https://agent.digen.ai/en"

try:                                    # same cookie loader as the movie worker
    from cookies_util import load_cookies
except ImportError:
    def load_cookies(path):
        """Fallback loader for EditThisCookie / Playwright cookie exports."""
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        same = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
        out = []
        for c in data:
            ck = {"name": c["name"], "value": c["value"], "path": c.get("path", "/")}
            if c.get("domain"):
                ck["domain"] = c["domain"]
            elif c.get("url"):
                ck["url"] = c["url"]
            exp = c.get("expirationDate", c.get("expires"))
            if isinstance(exp, (int, float)) and exp > 0:
                ck["expires"] = exp
            if c.get("httpOnly") is not None:
                ck["httpOnly"] = bool(c["httpOnly"])
            if c.get("secure") is not None:
                ck["secure"] = bool(c["secure"])
            ss = same.get(str(c.get("sameSite", "")).lower())
            if ss:
                ck["sameSite"] = ss
            out.append(ck)
        return out

# Credit balance shown in the header: <span title="Meme"> ... <span class="tabular-nums">249</span>
CREDITS_JS = """
() => {
  const el = document.querySelector('span[title="Meme"] span.tabular-nums')
          || document.querySelector('button span.tabular-nums')
          || document.querySelector('span.tabular-nums');
  if (el) {
    const n = parseInt(el.textContent.replace(/[^0-9]/g, ''), 10);
    return Number.isNaN(n) ? null : n;
  }
  return null;
}
"""

# Every visible clickable thing with its text and position.
CLICKABLES_JS = """
() => {
  const out = [], seen = new Set();
  document.querySelectorAll('button, [role="button"], [role="menuitem"], a, div[class*="cursor-pointer"]').forEach(e => {
    const r = e.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return;
    const t = (e.innerText || e.getAttribute('aria-label') || e.title || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 60) return;
    const key = t + '|' + Math.round(r.left) + '|' + Math.round(r.top);
    if (seen.has(key)) return;
    seen.add(key);
    const cls = (e.className && e.className.toString()) || '';
    out.push({t: t, x: r.left + r.width / 2, y: r.top + r.height / 2, area: r.width * r.height,
              dis: !!e.disabled || cls.includes('cursor-not-allowed') || e.getAttribute('aria-disabled') === 'true'});
  });
  return out;
}
"""

# Words that mean "free credits to take" and words that must never be clicked.
BASE_CLAIM = r"claim|check[\s-]?in|collect|daily|receive|free\s+(credits?|meme)|get\s+(free\s+)?(credits?|meme)|redeem|reward"
SKIP_RE = re.compile(r"buy|purchase|upgrade|subscribe|pay|plan|pricing|top[\s-]?up|recharge|checkout|"
                     r"cancel|log\s?out|sign\s?out|delete|remove|invite|share|referral|contact|support|help",
                     re.I)
MAX_CLICKS = 6          # per account
LOG_FILE = None
VERBOSE = False


# ---------------------------------------------------------------- logging
def log(m, level="INFO"):
    for part in (str(m).splitlines() or [""]):
        line = f"{time.strftime('%H:%M:%S')} [{level}] {part}"
        print(line, file=sys.stderr, flush=True)
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass


def vlog(m):
    if VERBOSE:
        log(m, "DEBUG")


def snap(page, path):
    try:
        page.screenshot(path=str(path))
        vlog(f"screenshot saved: {path}")
    except Exception as e:
        log(f"screenshot failed ({path}): {e}", "WARN")


def fail(m):
    log(f"FAILED: {json.dumps(m) if not isinstance(m, str) else m}", "ERROR")
    print(json.dumps({"ok": False, "error": m}), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- helpers
def expand_cookie_args(items):
    out = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            out += [str(f) for f in sorted(p.glob("*.json"))]
        else:
            out.append(str(p))
    return list(dict.fromkeys(out))


def dismiss_popups(page):
    for name in ("Not now", "Close", "Got it", "Skip", "Later"):
        try:
            b = page.get_by_role("button", name=name, exact=True).first
            if b.is_visible():
                b.click(timeout=1500)
                log(f"closed popup: {name}")
                page.wait_for_timeout(500)
        except Exception:
            pass


def read_credits(page, wait_s=10, stable_reads=2):
    """Balance as int, or None. The value must repeat on consecutive reads (placeholders while loading are ignored)."""
    end = time.time() + wait_s
    last, count = None, 0
    while time.time() < end:
        try:
            n = page.evaluate(CREDITS_JS)
        except Exception:
            n = None
        if n is None:
            last, count = None, 0
        elif n == last:
            count += 1
            if count >= stable_reads:
                return n
        else:
            last, count = n, 1
        page.wait_for_timeout(1200)
    return last if count >= 1 else None


def scan_and_claim(page, claim_re, clicked, where):
    """Click free-credit buttons currently on screen. Returns the texts clicked."""
    done = []
    for _ in range(MAX_CLICKS):
        try:
            items = page.evaluate(CLICKABLES_JS)
        except Exception:
            break
        cands = [i for i in items if claim_re.search(i["t"]) and not SKIP_RE.search(i["t"]) and not i["dis"]
                 and i["t"].lower() not in clicked]
        if VERBOSE or not cands:
            texts = sorted({i["t"] for i in items})[:30]
            vlog(f"[{where}] clickable texts: {texts}")
        if not cands:
            break
        cands.sort(key=lambda i: i["area"])        # the innermost element first
        c = cands[0]
        clicked.add(c["t"].lower())
        log(f"[{where}] clicking: '{c['t']}'")
        try:
            page.mouse.click(c["x"], c["y"])
        except Exception as e:
            log(f"click failed: {e}", "WARN")
            continue
        done.append(c["t"])
        page.wait_for_timeout(2500)
        dismiss_popups(page)
    return done


def open_credits_menu(page):
    """Click the credit badge in the header; it usually opens the menu with daily rewards."""
    for sel in ('span[title="Meme"]', 'button:has(span.tabular-nums)', 'span.tabular-nums'):
        try:
            el = page.locator(sel).first
            if el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                log(f"opened the credits menu via {sel}")
                return True
        except Exception:
            continue
    log("could not open the credits menu", "WARN")
    return False


# ---------------------------------------------------------------- one account
def collect_one(browser, cookie_file, args, claim_re):
    tag = Path(cookie_file).stem
    outdir = Path(args.outdir)
    row = {"cookie_file": cookie_file, "account": tag, "before": None, "after": None, "gained": 0,
           "claimed": [], "status": "error", "error": ""}
    if not Path(cookie_file).exists():
        row["error"] = "cookie file not found"
        return row

    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = None
    try:
        cookies = load_cookies(cookie_file)
        ctx.add_cookies(cookies)
        log(f"loaded {len(cookies)} cookies from {cookie_file}")
        page = ctx.new_page()
        page.on("pageerror", lambda e: vlog(f"page error: {str(e)[:200]}"))

        page.goto(AGENT_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('div[role="textbox"], span.tabular-nums', timeout=20000)
        except Exception:
            log("page ready-selector not found within 20 s; continuing", "WARN")
        page.wait_for_timeout(2000)

        before = read_credits(page)
        row["before"] = before
        log(f"credits before: {before}")
        if before is None:
            snap(page, outdir / "shots" / f"{tag}_nologin.png")
            row["status"] = "not logged in"
            row["error"] = "could not read credits (cookies expired or page not loaded)"
            return row

        if not args.no_claim:
            clicked = set()
            # 1) a daily-reward popup often shows up right after loading
            row["claimed"] += scan_and_claim(page, claim_re, clicked, "page")
            dismiss_popups(page)
            # 2) the credits badge menu
            if open_credits_menu(page):
                snap(page, outdir / "shots" / f"{tag}_menu.png")
                row["claimed"] += scan_and_claim(page, claim_re, clicked, "menu")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            page.wait_for_timeout(3000)

        after = read_credits(page, wait_s=8)
        row["after"] = after if after is not None else before
        row["gained"] = (row["after"] - before) if row["after"] is not None else 0
        log(f"credits after: {row['after']} (gained {row['gained']})")
        snap(page, outdir / "shots" / f"{tag}.png")

        if args.no_claim:
            row["status"] = "balance only"
        elif row["gained"] > 0:
            row["status"] = f"claimed +{row['gained']}"
        elif row["claimed"]:
            row["status"] = "clicked, balance unchanged"
        else:
            row["status"] = "nothing to claim"
        return row
    except Exception as e:
        log("unexpected error:\n" + traceback.format_exc(), "ERROR")
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"
        if page is not None:
            snap(page, outdir / "shots" / f"{tag}_error.png")
        return row
    finally:
        ctx.close()


def append_history(outdir, rows):
    path = Path(outdir) / "history.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "account", "before", "after", "gained", "status", "clicked", "error"])
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            w.writerow([now, r["account"], r["before"], r["after"], r["gained"], r["status"],
                        "; ".join(r["claimed"]), r["error"]])


# ---------------------------------------------------------------- main
def main():
    global LOG_FILE, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", nargs="+", default=["digen_cookies.json"],
                    help="cookie files and/or folders (a folder = all *.json inside)")
    ap.add_argument("--outdir", default="outputs_credits")
    ap.add_argument("--no-claim", action="store_true", help="only read the balances, click nothing")
    ap.add_argument("--claim-words", default="", help="extra words (regex) that mark a free-credit button")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds to wait between accounts")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if args.log_file:
        LOG_FILE = args.log_file
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    log("==== credit collector started ====")
    log(f"python {platform.python_version()} on {platform.platform()}")
    cookie_files = expand_cookie_args(args.cookies)
    log(f"accounts: {len(cookie_files)}; claim={'no' if args.no_claim else 'yes'}; headed={args.headed}")
    if not cookie_files:
        fail("No cookie files found")
    (Path(args.outdir) / "shots").mkdir(parents=True, exist_ok=True)

    pattern = BASE_CLAIM + (("|" + args.claim_words) if args.claim_words.strip() else "")
    try:
        claim_re = re.compile(pattern, re.I)
    except re.error as e:
        fail(f"bad --claim-words pattern: {e}")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        try:
            for n, cf in enumerate(cookie_files, 1):
                log(f"=== account {n}/{len(cookie_files)}: {cf} ===")
                row = collect_one(browser, cf, args, claim_re)
                log(f"result: {row['status']}" + (f" ({row['error']})" if row["error"] else ""))
                rows.append(row)
                append_history(args.outdir, [row])
                if n < len(cookie_files):
                    time.sleep(args.pause)
        finally:
            browser.close()

    total_before = sum(r["before"] or 0 for r in rows)
    total_after = sum(r["after"] if r["after"] is not None else (r["before"] or 0) for r in rows)
    result = {"ok": True, "accounts": rows, "total_before": total_before, "total_after": total_after,
              "gained": sum(r["gained"] for r in rows), "seconds": int(time.time() - t0)}
    Path("result_credits.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"DONE: {len(rows)} accounts, gained {result['gained']} credits, total now {total_after}")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()