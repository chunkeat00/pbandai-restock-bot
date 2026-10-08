#!/usr/bin/env python3
"""
Toymana restock bot.

Watches one or more Shopify collections (default: Toymana's Beyblade
collection) and sends a Telegram message when a product appears or comes back
in stock.

Shopify serves every collection as JSON at /collections/<handle>/products.json,
including each variant's `available` flag, so there is nothing to render or
scrape: one request per 250 products. robots.txt allows that path (it only
disallows the `sort_by=` collection pages), and it is the same data the
storefront shows.

Unlike the KGB bot next door, sold-out products stay in the feed with
`available: false`, so availability is read per product rather than inferred
from presence. And unlike the P-Bandai bot, every product is recorded,
sold out or not: here sold-out items are recent stock that comes back, and a
product that was never recorded would be announced as new when it restocks.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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

DEFAULT_URLS = "https://www.toymana.com/collections/beyblade"
# `${{ vars.X }}` on an unset repo variable expands to an empty string, not to
# nothing — so an unset variable must fall back the same way a missing one does.
WATCH_URLS = [
    u.strip() for u in re.split(
        r"[\n,]+", os.environ.get("TOYMANA_WATCH_URLS", "").strip() or DEFAULT_URLS)
    if u.strip() and not u.strip().startswith("#")
]

STATE_FILE = Path(os.environ.get("STATE_FILE", "toymana-restock-bot/state/seen.json"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

PAGE_SIZE = 250            # Shopify's maximum for products.json
# A runaway guard, not a window: a collection longer than MAX_PAGES × 250
# fails loudly instead of being silently cut short.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "20"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.5"))

# Dead man's switch (healthchecks.io ping URL). Optional — unset means off.
# The only thing that catches a *silent* stop: expired PAT, dead trigger,
# runner that never started. Nothing in here can report those. It also catches
# the site staying unreadable: such runs send no ping (see the end of this file).
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CURRENCY_SYMBOL = {"SGD": "SG$", "MYR": "RM", "AUD": "AU$", "HKD": "HK$",
                   "USD": "US$", "TWD": "NT$"}

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def fetch_json(url: str, attempts: int = 3) -> dict | None:
    """GET a JSON document, or None once retries are spent. A storefront that
    is down, password-protected or challenging bots answers with HTML, which
    fails to parse and lands here as None — never as an empty collection."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(data, dict):
                return data
            print(f"     unexpected JSON shape from {url}", file=sys.stderr)
        except Exception as e:
            print(f"     fetch attempt {i + 1} failed: {e}", file=sys.stderr)
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    return None


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #

COLLECTION_PATH = re.compile(r"^/collections/([^/?#]+)")


def parse_collection(url: str) -> tuple[str, str] | None:
    """(host, handle) for a collection URL; query strings like sort_by are
    ignored — they only change the storefront's ordering, not its contents."""
    u = urllib.parse.urlsplit(url)
    m = COLLECTION_PATH.match(u.path)
    if not u.netloc or not m:
        return None
    return u.netloc.lower(), m.group(1).lower()


def group_of(host: str, handle: str) -> str:
    """e.g. "toymana.com/beyblade". Namespaces state keys per store+collection
    and names the collection in failure messages."""
    return f"{host.removeprefix('www.')}/{handle}"


def store_currency(host: str) -> str:
    """Price prefix from the store's /meta.json. Cosmetic: on any failure the
    price is shown without one rather than failing the run."""
    meta = fetch_json(f"https://{host}/meta.json", attempts=1)
    code = (meta or {}).get("currency") or ""
    return CURRENCY_SYMBOL.get(code, f"{code} " if code else "")


def to_item(p: dict, group: str, host: str, cur: str) -> dict | None:
    if not p.get("id") or not p.get("handle"):
        return None
    variants = p.get("variants") or []
    in_stock = [v for v in variants if v.get("available")]

    # Cheapest variant you can actually buy; if nothing is buyable, the
    # cheapest overall, so a sold-out record still carries a price.
    price = ""
    pool = [v for v in (in_stock or variants) if v.get("price") is not None]
    try:
        if pool:
            price = f"{cur}{min(float(v['price']) for v in pool):,.2f}"
    except (TypeError, ValueError):
        pass

    # Only worth naming when the product actually has a choice, e.g. launcher
    # colours. Single-variant products are titled "Default Title".
    names = [v["title"] for v in in_stock
             if v.get("title") and v["title"] != "Default Title"]

    return {
        "key": f"{group}:{p['id']}",
        "group": group,
        "title": (p.get("title") or p["handle"]).strip()[:200],
        "url": f"https://{host}/products/{p['handle']}",
        "price": price,
        "available": bool(in_stock),
        "variants": names if len(variants) > 1 else [],
    }


def scrape_group(url: str) -> tuple[list[dict], bool]:
    """Read one collection to the end. Returns (items, ok).

    Shopify gives no total count, so completeness rests on the last page being
    short: a full page means there may be more, so we keep going. Any page
    that fails makes the whole collection a failure — a partial read would
    mark whatever it missed as gone.
    """
    host, handle = parse_collection(url)
    group = group_of(host, handle)
    print(f"[scrape] {url}", flush=True)

    products: dict[int, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(REQUEST_DELAY)
        data = fetch_json(f"https://{host}/collections/{handle}/products.json"
                          f"?limit={PAGE_SIZE}&page={page}")
        batch = (data or {}).get("products")
        if not isinstance(batch, list):
            print(f"     page {page}: no product feed — treating as failure",
                  file=sys.stderr)
            return [], False
        for p in batch:
            if p.get("id"):
                products.setdefault(p["id"], p)
        if len(batch) < PAGE_SIZE:
            break
    else:
        print(f"     more than {MAX_PAGES * PAGE_SIZE} products — raise "
              "MAX_PAGES; treating as failure rather than reading a prefix",
              file=sys.stderr)
        return [], False

    if not products:
        # The collection had products last time; an empty feed is far more
        # likely a broken response than every product being delisted at once.
        # Trusting it would delist the lot and announce it all as restocks
        # when it came back.
        print("     empty collection — treating as failure", file=sys.stderr)
        return [], False

    cur = store_currency(host)
    items = [it for p in products.values() if (it := to_item(p, group, host, cur))]
    avail = sum(1 for it in items if it["available"])
    print(f"     products={len(items)} in_stock={avail}", flush=True)
    return items, True


def scrape_all() -> tuple[list[dict], dict[str, bool]]:
    all_items: list[dict] = []
    ok_by_group: dict[str, bool] = {}
    for url in WATCH_URLS:
        group = group_of(*parse_collection(url))
        try:
            items, ok = scrape_group(url)
        except Exception as e:
            items, ok = [], False
            print(f"  !! failed: {e}", file=sys.stderr)
        all_items.extend(items)
        ok_by_group[group] = ok_by_group.get(group, True) and ok
    return all_items, ok_by_group


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"items": {}, "initialized": False, "updated_at": None}
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


# A run in which nothing at all could be read. Not worth an immediate alarm:
# see the end of this file.
UNREADABLE = 3


def fingerprint(state: dict) -> str:
    """The state minus what changes on every run (last_seen, updated_at). A run
    that only re-confirmed what we already knew compares equal, writes nothing,
    and so leaves the workflow nothing to commit — at several runs an hour,
    committing timestamps alone would mean dozens of commits a day per bot."""
    items = {k: {f: v for f, v in r.items() if f != "last_seen"}
             for k, r in (state.get("items") or {}).items()}
    rest = {k: v for k, v in state.items() if k not in ("items", "updated_at")}
    return json.dumps([items, rest], sort_keys=True, ensure_ascii=False)


def on_disk_fingerprint() -> str | None:
    """Fingerprint of the state file as committed — before this run's
    migrations or changes — or None if there is no readable file."""
    try:
        return fingerprint(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return None


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
    return htmllib.escape(s or "", quote=False)


def fmt_item(it: dict, tag: str) -> str:
    bits = [f"{tag} <b>{esc(it['title'])}</b>"]
    meta = " · ".join(x for x in (
        it.get("price"),
        ("有货规格：" + " / ".join(it["variants"])) if it.get("variants") else "",
    ) if x)
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
# Dead man's switch
# --------------------------------------------------------------------------- #

def hc_ping(suffix: str = "", body: str = "") -> None:
    """Ping healthchecks.io. Silence is the alert, so this must never raise and
    never change the exit code."""
    if not HEALTHCHECK_URL or DRY_RUN:
        return
    url = HEALTHCHECK_URL.rstrip("/") + suffix
    try:
        req = urllib.request.Request(url, data=body.encode()[:10000] or None)
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"healthcheck ping failed ({suffix or '/'}): {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    bad_urls = [u for u in WATCH_URLS if not parse_collection(u)]
    if not WATCH_URLS or bad_urls:
        print(f"TOYMANA_WATCH_URLS must be Shopify collection URLs "
              f"(…/collections/<name>), got: {bad_urls or 'nothing'}",
              file=sys.stderr)
        return 2

    if not DRY_RUN and (not BOT_TOKEN or not CHAT_IDS):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return 2

    bad_ids = [c for c in CHAT_IDS if not re.fullmatch(r"-?\d+|@[\w]{5,}", c)]
    if bad_ids:
        print(f"TELEGRAM_CHAT_ID has malformed entries: {bad_ids}",
              file=sys.stderr)
        return 2

    print("[config] watching:\n  " + "\n  ".join(WATCH_URLS), flush=True)
    print(f"[config] notifying {len(CHAT_IDS)} chat(s)", flush=True)

    items, ok_by_group = scrape_all()
    good = {g for g, ok in ok_by_group.items() if ok}
    bad = sorted(g for g, ok in ok_by_group.items() if not ok)

    items = [it for it in items if it["group"] in good]
    in_stock = [it for it in items if it["available"]]
    print(f"[scrape] ok={sorted(good) or '-'} failed={bad or '-'} "
          f"products={len(items)} in_stock={len(in_stock)}", flush=True)

    if not good:
        print("no collection read cleanly — state untouched", file=sys.stderr)
        # Annotates the run's summary page; the run itself stays green.
        print("::warning::nothing could be read this run — state untouched",
              flush=True)
        return UNREADABLE

    before = on_disk_fingerprint()
    state = load_state()
    known: dict = state.get("items", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items, back_items = [], []

    # Every product is recorded, in stock or not — see the module docstring.
    for it in items:
        prev = known.get(it["key"])
        if it["available"]:
            if prev is None:
                new_items.append(it)
            elif not prev.get("present", False):
                back_items.append(it)

        known[it["key"]] = {
            "group": it["group"],
            "title": it["title"],
            "url": it["url"],
            "price": it["price"],
            "present": it["available"],
            "first_seen": (prev or {}).get("first_seen", now),
            "last_seen": now,
        }

    # Gone from a collection we read cleanly = delisted, which for our purposes
    # is the same as sold out. Records in a failed collection are left as they
    # were.
    read_keys = {it["key"] for it in items}
    for key, rec in known.items():
        group = rec.get("group") or key.rsplit(":", 1)[0]
        if group in good and key not in read_keys:
            rec["present"] = False

    if not state.get("initialized"):
        tg_send(
            "✅ <b>Toymana restock bot 已启动</b>\n"
            f"监控中：{len(WATCH_URLS)} 个分类，共 {len(items)} 件商品，"
            f"其中现在有货 {len(in_stock)} 件。\n"
            + (f"⚠️ 读不到：{', '.join(bad)}\n" if bad else "")
            + "之后有上新或补货才会再通知你。"
        )
    else:
        if new_items:
            send_batched(f"🆕 <b>Toymana 上新 {len(new_items)} 件</b>",
                         [fmt_item(i, "🔹") for i in new_items])
        if back_items:
            send_batched(f"♻️ <b>Toymana 补货 {len(back_items)} 件</b>",
                         [fmt_item(i, "🔸") for i in back_items])
        if not new_items and not back_items:
            print("no changes", flush=True)

    prev_bad = set(state.get("failed_groups") or [])
    if state.get("initialized") and set(bad) != prev_bad:
        if bad:
            tg_send(
                "⚠️ <b>Toymana 抓取异常</b>\n"
                f"读不到：{', '.join(bad)}\n"
                "这些分类的记录已冻结，不会误报上新或补货。"
            )
        else:
            tg_send(f"✅ <b>Toymana 抓取已恢复</b>\n{', '.join(sorted(prev_bad))} 恢复正常。")

    state["items"] = known
    state["initialized"] = True
    state["failed_groups"] = bad
    if fingerprint(state) != before:
        save_state(state)
    else:
        print("nothing changed beyond timestamps — state file left as is",
              flush=True)

    print(f"[done] new={len(new_items)} back={len(back_items)} "
          f"tracked={len(known)} failed={bad or '-'}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        # A crash is a bug, not a bad minute: say so at once.
        hc_ping("/fail", traceback.format_exc())
        raise
    if code == UNREADABLE:
        # Every site unreadable this run. Usually a blip on their side or ours
        # that the next run does not see — so no alarm, and
        # no success ping either. If it keeps happening, the missing pings turn
        # the dead man's switch red once its grace period runs out.
        sys.exit(0)
    hc_ping("" if code == 0 else "/fail", f"exit={code}")
    sys.exit(code)
