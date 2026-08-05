#!/usr/bin/env python3
"""
P-Bandai restock / new-arrival alert bot.

Renders the P-Bandai listing page(s) with a headless browser, extracts every
product card from the results grid, keeps only the orderable ones, diffs
against a saved state file, and pushes a Telegram message when something new
shows up (or an item comes back in stock).

Why we filter in the scraper instead of trusting the URL:
  P-Bandai's `_f_productStatuses` query param is unreliable. Verified 2026-08:
  on the AU site `_f_productStatuses=Waiting,On` returns 19 results that are
  ALL "OUT OF STOCK" / "PRE-ORDER CLOSED", while `_f_productStatuses=On`
  correctly returns 0. So the availability decision is made here, from the
  badge printed on each card.

Env vars:
  TELEGRAM_BOT_TOKEN   (required)
  TELEGRAM_CHAT_ID     (required) one or more chat ids, comma- or
                       newline-separated. Groups/channels are negative ids.
  WATCH_URLS           (required) newline- or comma-separated listing URLs.
                       No default: an unset value aborts the run instead of
                       quietly falling back to URLs baked into this file.
  STATE_FILE           (optional) default: state/seen.json
  MAX_PAGES            (optional) default: 5
  ALERT_ON_ALL         (optional) "1" = also alert on unavailable items
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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# One or more recipients, comma- or newline-separated. A group id is negative
# (e.g. -1001234567890), so we must not strip leading "-".
CHAT_IDS = [
    c.strip() for c in re.split(r"[\n,;]+",
                                os.environ.get("TELEGRAM_CHAT_ID", ""))
    if c.strip() and not c.strip().startswith("#")
]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
ALERT_ON_ALL = os.environ.get("ALERT_ON_ALL", "") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# No hardcoded fallback on purpose. If WATCH_URLS is unset the run aborts
# loudly, rather than silently monitoring URLs baked into the source that you
# forgot were there.
RAW_URLS = os.environ.get("WATCH_URLS", "").strip()
WATCH_URLS = [
    u.strip() for u in re.split(r"[\n,]+", RAW_URLS)
    if u.strip() and not u.strip().startswith("#")
]

WATCH_URLS_HELP = """\
WATCH_URLS is not set — nothing to monitor.

Set it as a GitHub repository *variable*:
  repo -> Settings -> Secrets and variables -> Actions -> Variables tab
  -> New repository variable -> name: WATCH_URLS

One listing URL per line, e.g.:
  https://p-bandai.com/sg/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival
  https://p-bandai.com/au/series/onepiece-series?_f_series=03-002&offset=0&limit=20&sortType=NewArrival

Leave out _f_productStatuses — that filter is unreliable on P-Bandai and this
script decides availability from each card's badge instead.

Locally:  export WATCH_URLS='<url1>
<url2>'
"""

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

# A card carrying any of these badges cannot be ordered right now.
# Everything else -- PRE-ORDER, IN STOCK, COMING SOON, or no badge at all --
# counts as available. Deny-list rather than allow-list, so an unfamiliar but
# orderable badge still triggers an alert instead of being silently dropped.
UNAVAILABLE_MARKERS = (
    "OUT OF STOCK",
    "SOLD OUT",
    "CLOSED",              # covers "PRE-ORDER CLOSED"
    "NO LONGER AVAILABLE",
    "END OF SALE",
    "SALE ENDED",
    "ENDED",
    "SUSPENDED",
    "CANCELLED",
    "CANCELED",
    "NOT AVAILABLE",
)


def is_available(item: dict) -> bool:
    blob = " ".join(item.get("flags") or []).upper()
    return not any(mark in blob for mark in UNAVAILABLE_MARKERS)


def region_of(url: str) -> str:
    """https://p-bandai.com/sg/series/... -> 'sg'"""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    return parts[0].lower() if parts else "xx"


# --------------------------------------------------------------------------- #
# Page extraction
# --------------------------------------------------------------------------- #

# Runs inside the page.
#
# Scoped to `.o-search-product`, the real results grid. The page ALSO renders a
# "RECOMMENDATIONS" carousel (`.c-search-recommend-carousel__slide-list`) full
# of unrelated products -- scanning the whole document picks those up and
# produces junk alerts every run.
#
# Returns {ok, items}. `ok` reports that the grid rendered at all, so a
# legitimately empty filter (0 available items) can be told apart from a
# broken scrape.
EXTRACT_JS = r"""
() => {
  const root = document.querySelector('.o-search-product');
  if (!root) return { ok: false, items: [] };

  const ITEM_SEL = 'a[href*="/item/"]';
  const idOf = href => {
    const m = href.match(/\/item\/([A-Za-z0-9][A-Za-z0-9_-]*)/);
    return m ? m[1] : null;
  };

  let cards = Array.from(root.querySelectorAll('.c-product'));

  // Fallback if Bandai renames the card class: smallest single-item ancestor.
  if (!cards.length) {
    const seen = new Set();
    for (const a of root.querySelectorAll(ITEM_SEL)) {
      let card = a;
      for (let i = 0; i < 5; i++) {
        const p = card.parentElement;
        if (!p || p === root) break;
        const ids = new Set();
        for (const l of p.querySelectorAll(ITEM_SEL)) {
          const id = idOf(l.href);
          if (id) ids.add(id);
        }
        if (ids.size > 1) break;
        card = p;
      }
      if (!seen.has(card)) { seen.add(card); cards.push(card); }
    }
  }

  const txt = el => ((el && el.innerText) || '')
    .replace(/ /g, ' ').replace(/\s+/g, ' ').trim();

  const results = {};

  for (const card of cards) {
    const a = card.matches && card.matches(ITEM_SEL)
      ? card : card.querySelector(ITEM_SEL);
    if (!a) continue;
    const id = idOf(a.href);
    if (!id) continue;

    const img = card.querySelector('img');

    let title = txt(card.querySelector('.c-product__title'));
    if (!title) title = txt(a).split('\n')[0];
    if (!title && img && img.alt) title = img.alt.trim();

    let price = txt(card.querySelector('.c-product__price'));
    if (!price) {
      const pm = txt(card).match(/(?:S\$|A\$|SGD|AUD|\$)\s?[\d,]+(?:\.\d{2})?/);
      price = pm ? pm[0].replace(/\s+/g, '') : '';
    }

    // Status badges, e.g. PRE-ORDER / OUT OF STOCK / PRE-ORDER CLOSED.
    let flags = Array.from(card.querySelectorAll('.p-flag__item'))
      .map(f => txt(f)).filter(Boolean);
    if (!flags.length) {
      const rest = txt(card).replace(title, '').trim();
      if (rest && rest.length < 40) flags = [rest];
    }

    const prev = results[id];
    const score = title.length + price.length + flags.join('').length;
    if (!prev || score > prev._score) {
      results[id] = {
        id,
        title: title.slice(0, 200),
        url: a.href.split('?')[0],
        image: img ? (img.currentSrc || img.src || '') : '',
        price,
        flags,
        _score: score,
      };
    }
  }

  return {
    ok: true,
    items: Object.values(results).map(o => { delete o._score; return o; }),
  };
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
    for label in ("Accept", "I Agree", "Agree", "OK", "Close", "Reject All"):
        try:
            btn = page.get_by_role(
                "button", name=re.compile(rf"^\s*{label}\s*$", re.I)
            )
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def scrape_url(page, url: str) -> tuple[list[dict], bool]:
    """Scrape one listing URL across offset pages. Returns (items, ok)."""
    limit = page_limit(url)
    region = region_of(url)
    found: dict[str, dict] = {}
    any_ok = False

    for page_idx in range(MAX_PAGES):
        target = set_offset(url, page_idx * limit)
        print(f"  -> [{region}] offset={page_idx * limit}", flush=True)

        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=25_000)
        except Exception:
            pass

        if page_idx == 0:
            dismiss_overlays(page)

        # Wait for the results grid, then nudge lazy images/cards.
        try:
            page.wait_for_selector(".o-search-product", timeout=20_000)
        except Exception:
            print("     results grid never appeared", file=sys.stderr)
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(600)
        page.wait_for_timeout(1000)

        try:
            res = page.evaluate(EXTRACT_JS)
        except Exception as e:
            print(f"     extraction failed: {e}", file=sys.stderr)
            res = {"ok": False, "items": []}

        ok = bool(res.get("ok"))
        items = res.get("items") or []
        any_ok = any_ok or ok

        fresh = 0
        for it in items:
            it["region"] = region
            it["key"] = f"{region}:{it['id']}"
            if it["key"] not in found:
                found[it["key"]] = it
                fresh += 1

        avail = sum(1 for it in items if is_available(it))
        print(f"     grid_ok={ok} items={len(items)} available={avail} "
              f"new_on_page={fresh}", flush=True)

        if not ok or fresh == 0 or len(items) < limit:
            break

    return list(found.values()), any_ok


def scrape_all() -> tuple[list[dict], bool]:
    all_items: dict[str, dict] = {}
    all_ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 1000},
            locale="en-SG",
        )
        page = ctx.new_page()
        for url in WATCH_URLS:
            print(f"[scrape] {url}", flush=True)
            try:
                items, ok = scrape_url(page, url)
                all_ok = all_ok and ok
                for it in items:
                    all_items.setdefault(it["key"], it)
            except Exception as e:
                all_ok = False
                print(f"  !! failed: {e}", file=sys.stderr)
        browser.close()
    return list(all_items.values()), all_ok


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"items": {}, "initialized": False, "updated_at": None}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        print("state file unreadable, starting fresh", file=sys.stderr)
        return {"items": {}, "initialized": False, "updated_at": None}

    # Migration: v1 keyed items by bare id (SG-only). v2 keys them by
    # "<region>:<id>" so the same id on two storefronts stays distinct.
    items = state.get("items", {})
    if items and any(":" not in k for k in items):
        migrated = {}
        for k, v in items.items():
            migrated[k if ":" in k else f"sg:{k}"] = v
        state["items"] = migrated
        print(f"migrated {len(migrated)} state keys to region-scoped form",
              flush=True)
    return state


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

def _tg_send_one(chat_id: str, text: str) -> bool:
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode()

    for attempt in range(3):
        req = urllib.request.Request(
            api, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = json.loads(r.read().decode())
            if body.get("ok"):
                return True
            desc = str(body.get("description", body))
            print(f"telegram error for {chat_id}: {desc}", file=sys.stderr)
            # Permanent failures: retrying will not help.
            if any(s in desc.lower() for s in
                   ("chat not found", "blocked", "kicked", "deactivated",
                    "not enough rights", "bot was")):
                return False
        except Exception as e:
            print(f"telegram attempt {attempt + 1} for {chat_id} failed: {e}",
                  file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return False


def tg_send(text: str) -> None:
    """Send to every configured recipient. One bad chat id must not stop
    delivery to the others."""
    if DRY_RUN or not BOT_TOKEN or not CHAT_IDS:
        print(f"--- would send to {len(CHAT_IDS) or 0} chat(s) ---\n"
              + text + "\n------------------", flush=True)
        return

    failed = [cid for cid in CHAT_IDS if not _tg_send_one(cid, text)]
    if failed:
        print(f"delivery failed for: {', '.join(failed)}", file=sys.stderr)


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def fmt_item(it: dict, tag: str) -> str:
    region = (it.get("region") or "").upper()
    head = f"{tag} [{region}] <b>{esc(it['title'] or it['id'])}</b>"
    bits = [head]
    meta = " · ".join(
        x for x in (it.get("price"), " / ".join(it.get("flags") or [])) if x
    )
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
    if not WATCH_URLS:
        print(WATCH_URLS_HELP, file=sys.stderr)
        return 2

    bad = [u for u in WATCH_URLS if not u.startswith(("http://", "https://"))]
    if bad:
        print(f"WATCH_URLS contains non-URL entries: {bad}", file=sys.stderr)
        return 2

    if not DRY_RUN and (not BOT_TOKEN or not CHAT_IDS):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    bad_ids = [c for c in CHAT_IDS if not re.fullmatch(r"-?\d+|@[\w]{5,}", c)]
    if bad_ids:
        print(f"TELEGRAM_CHAT_ID has malformed entries: {bad_ids}\n"
              "Expected numeric ids (groups are negative, e.g. -1001234567890) "
              "or public @channelname.", file=sys.stderr)
        return 2

    print("[config] watching:\n  " + "\n  ".join(WATCH_URLS), flush=True)
    print(f"[config] notifying {len(CHAT_IDS)} chat(s)", flush=True)

    items, ok = scrape_all()
    available = [it for it in items if ALERT_ON_ALL or is_available(it)]
    print(f"[scrape] grid_ok={ok} scraped={len(items)} "
          f"alertable={len(available)}", flush=True)

    if not ok:
        # Layout change, bot block, or network trouble. Bail without touching
        # state so the next good run doesn't report the whole catalogue as new.
        print("results grid missing on at least one URL — state untouched",
              file=sys.stderr)
        return 1

    state = load_state()
    known: dict = state.get("items", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items, back_items = [], []
    alertable_keys = {it["key"] for it in available}

    # Record every scraped item, available or not. `present` tracks
    # alertability, so sold-out -> back-in-stock reads as a restock.
    for it in items:
        key = it["key"]
        prev = known.get(key)
        now_alertable = key in alertable_keys

        if now_alertable:
            if prev is None:
                new_items.append(it)
            elif not prev.get("present", False):
                back_items.append(it)

        known[key] = {
            "region": it["region"],
            "title": it["title"],
            "url": it["url"],
            "price": it.get("price", ""),
            "flags": it.get("flags", []),
            "present": now_alertable,
            "first_seen": (prev or {}).get("first_seen", now),
            "last_seen": now,
        }

    scraped_keys = {it["key"] for it in items}
    for key, rec in known.items():
        if key not in scraped_keys:
            rec["present"] = False

    if not state.get("initialized"):
        tg_send(
            "✅ <b>P-Bandai restock bot 已启动</b>\n"
            f"监控中：{len(WATCH_URLS)} 个列表，共 {len(known)} 件商品，"
            f"其中现在可下单 {len(available)} 件。\n"
            "之后有上新或补货才会再通知你。"
        )
    else:
        if new_items:
            send_batched(f"🆕 <b>P-Bandai 上新 {len(new_items)} 件</b>",
                         [fmt_item(i, "🔹") for i in new_items])
        if back_items:
            send_batched(f"♻️ <b>P-Bandai 补货 {len(back_items)} 件</b>",
                         [fmt_item(i, "🔸") for i in back_items])
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
