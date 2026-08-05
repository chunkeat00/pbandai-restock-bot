#!/usr/bin/env python3
"""
P-Bandai restock / new-arrival alert bot.

Renders the P-Bandai listing page(s) with a headless browser, extracts every
product card, diffs against a saved state file, and pushes a Telegram message
when something new appears (or an item comes back into the list = restock).

Env vars:
  TELEGRAM_BOT_TOKEN   (required)
  TELEGRAM_CHAT_ID     (required)
  WATCH_URLS           (optional) newline- or comma-separated list of listing
                       URLs to watch. Defaults to the One Piece SG list.
  STATE_FILE           (optional) default: state/seen.json
  MAX_PAGES            (optional) default: 5
  DRY_RUN              (optional) "1" = print instead of sending
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_URL = (
    "https://p-bandai.com/sg/series/onepiece-series"
    "?_f_series=03-002&offset=0&limit=20"
    "&sortType=NewArrival&_f_productStatuses=Waiting,On"
)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

RAW_URLS = os.environ.get("WATCH_URLS", "").strip()
if RAW_URLS:
    WATCH_URLS = [u.strip() for u in re.split(r"[\n,]+", RAW_URLS) if u.strip()]
else:
    WATCH_URLS = [DEFAULT_URL]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Page extraction
# --------------------------------------------------------------------------- #

# Runs inside the page. Finds every <a> pointing at an item detail page and
# walks up to the enclosing card to scrape title / price / status / image.
EXTRACT_JS = r"""
() => {
  const results = {};
  const anchors = Array.from(document.querySelectorAll('a[href*="/item/"]'));

  for (const a of anchors) {
    const m = a.href.match(/\/item\/([A-Za-z0-9][A-Za-z0-9_-]*)/);
    if (!m) continue;
    const id = m[1];

    // Card boundary = the largest ancestor that still contains only THIS item.
    // Going one level further would swallow a neighbouring product.
    let card = a;
    for (let i = 0; i < 6; i++) {
      const p = card.parentElement;
      if (!p || p === document.body) break;
      const ids = new Set();
      for (const l of p.querySelectorAll('a[href*="/item/"]')) {
        const mm = l.href.match(/\/item\/([A-Za-z0-9][A-Za-z0-9_-]*)/);
        if (mm) ids.add(mm[1]);
      }
      if (ids.size > 1) break;
      card = p;
    }

    const text = (card.innerText || '').replace(/ /g, ' ').trim();
    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    const img = card.querySelector('img');

    // Title: prefer the anchor's own text, then img alt, then longest line.
    let title = (a.innerText || '').trim().split('\n').map(s => s.trim())
                  .filter(Boolean).sort((x, y) => y.length - x.length)[0] || '';
    if (title.length < 5 && img && img.alt) title = img.alt.trim();
    if (title.length < 5 && lines.length) {
      title = lines.slice().sort((x, y) => y.length - x.length)[0];
    }

    // Price: first money-looking token.
    let price = '';
    const pm = text.match(/(?:S\$|SGD|\$)\s?[\d,]+(?:\.\d{2})?/);
    if (pm) price = pm[0].replace(/\s+/g, '');

    // Status badges.
    let status = '';
    const statusPatterns = [
      'Sold Out', 'Order Period', 'Pre-order', 'Preorder', 'Coming Soon',
      'On Sale', 'In Stock', 'Order Now', 'Accepting', 'Waiting',
      'Reservation', 'New Arrival'
    ];
    for (const p of statusPatterns) {
      const rx = new RegExp(p.replace(/\s+/g, '\\s+'), 'i');
      const hit = text.match(rx);
      if (hit) { status = hit[0]; break; }
    }

    // Keep the richest record if the same id shows up twice.
    const prev = results[id];
    const score = title.length + price.length + status.length;
    if (!prev || score > prev._score) {
      results[id] = {
        id,
        title: title.slice(0, 200),
        url: a.href.split('?')[0],
        image: img ? img.src : '',
        price,
        status,
        _score: score,
      };
    }
  }

  return Object.values(results).map(o => { delete o._score; return o; });
}
"""


def set_offset(url: str, offset: int) -> str:
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k != "offset"]
    q.append(("offset", str(offset)))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(q, safe=","), parts.fragment)
    )


def page_limit(url: str) -> int:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    try:
        return max(1, int(q.get("limit", "20")))
    except ValueError:
        return 20


def dismiss_overlays(page) -> None:
    """Best-effort click on cookie / region / age-gate banners."""
    labels = [
        "Accept", "ACCEPT", "I Agree", "Agree", "OK", "Close",
        "Reject All", "Decline", "同意", "閉じる",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{label}\s*$", re.I))
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def scrape_url(page, url: str) -> list[dict]:
    """Scrape one listing URL, following offset pagination."""
    limit = page_limit(url)
    found: dict[str, dict] = {}

    for page_idx in range(MAX_PAGES):
        target = set_offset(url, page_idx * limit)
        print(f"  -> fetching offset={page_idx * limit}", flush=True)

        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            pass

        if page_idx == 0:
            dismiss_overlays(page)

        # Give the SPA a moment and trigger any lazy loading.
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        page.wait_for_timeout(1200)

        try:
            items = page.evaluate(EXTRACT_JS)
        except Exception as e:
            print(f"     extraction failed: {e}", file=sys.stderr)
            items = []

        fresh = [it for it in items if it["id"] not in found]
        for it in items:
            found.setdefault(it["id"], it)

        print(f"     {len(items)} on page, {len(fresh)} new", flush=True)

        # Stop when a page adds nothing or returns a short page.
        if not fresh or len(items) < limit:
            break

    return list(found.values())


def scrape_all() -> list[dict]:
    all_items: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-SG",
        )
        page = ctx.new_page()
        for url in WATCH_URLS:
            print(f"[scrape] {url}", flush=True)
            try:
                for it in scrape_url(page, url):
                    all_items.setdefault(it["id"], it)
            except Exception as e:
                print(f"  !! failed: {e}", file=sys.stderr)
        browser.close()
    return list(all_items.values())


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            print("state file unreadable, starting fresh", file=sys.stderr)
    return {"items": {}, "initialized": False, "updated_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def tg_send(text: str) -> None:
    if DRY_RUN or not BOT_TOKEN or not CHAT_ID:
        print("--- would send ---\n" + text + "\n------------------", flush=True)
        return

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode()

    req = urllib.request.Request(
        api, data=payload, headers={"Content-Type": "application/json"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = json.loads(r.read().decode())
            if body.get("ok"):
                return
            print(f"telegram error: {body}", file=sys.stderr)
        except Exception as e:
            print(f"telegram attempt {attempt + 1} failed: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def fmt_item(it: dict, tag: str) -> str:
    bits = [f"{tag} <b>{esc(it['title'] or it['id'])}</b>"]
    meta = " · ".join(x for x in (it.get("price"), it.get("status")) if x)
    if meta:
        bits.append(esc(meta))
    bits.append(it["url"])
    return "\n".join(bits)


def send_batched(header: str, blocks: list[str]) -> None:
    chunk, size = [], len(header) + 2
    for b in blocks:
        if chunk and (size + len(b) + 2 > 3500 or len(chunk) >= 8):
            tg_send(header + "\n\n" + "\n\n".join(chunk))
            chunk, size = [], len(header) + 2
        chunk.append(b)
        size += len(b) + 2
    if chunk:
        tg_send(header + "\n\n" + "\n\n".join(chunk))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    items = scrape_all()
    print(f"[scrape] total unique items: {len(items)}", flush=True)

    if not items:
        # Never wipe state on a bad scrape (site down, layout change, bot block).
        print("no items found — treating as a failed run, state untouched",
              file=sys.stderr)
        return 1

    state = load_state()
    known: dict = state.get("items", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items, back_items = [], []
    current_ids = set()

    for it in items:
        iid = it["id"]
        current_ids.add(iid)
        prev = known.get(iid)

        if prev is None:
            new_items.append(it)
        elif not prev.get("present", True):
            back_items.append(it)

        known[iid] = {
            "title": it["title"],
            "url": it["url"],
            "price": it.get("price", ""),
            "status": it.get("status", ""),
            "present": True,
            "first_seen": (prev or {}).get("first_seen", now),
            "last_seen": now,
        }

    for iid, rec in known.items():
        if iid not in current_ids:
            rec["present"] = False

    first_run = not state.get("initialized")

    if first_run:
        tg_send(
            "✅ <b>P-Bandai restock bot 已启动</b>\n"
            f"目前在监控 {len(current_ids)} 件商品。\n"
            "之后有上新或补货才会再通知你。"
        )
    else:
        if new_items:
            send_batched(
                f"🆕 <b>P-Bandai 上新 {len(new_items)} 件</b>",
                [fmt_item(i, "🔹") for i in new_items],
            )
        if back_items:
            send_batched(
                f"♻️ <b>P-Bandai 补货 {len(back_items)} 件</b>",
                [fmt_item(i, "🔸") for i in back_items],
            )
        if not new_items and not back_items:
            print("no changes", flush=True)

    state["items"] = known
    state["initialized"] = True
    save_state(state)

    print(f"[done] new={len(new_items)} back={len(back_items)} "
          f"tracked={len(known)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
