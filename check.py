#!/usr/bin/env python3
"""
P-Bandai restock / new-arrival alert bot.

Reads the P-Bandai listing page(s), keeps only the orderable products, diffs
against a saved state file, and pushes a Telegram message when something new
shows up (or an item comes back in stock).

No browser. P-Bandai's listing HTML already carries the full search result as
JSON, in a `PRELOAD_DATA = {...}` script the server writes before any
JavaScript runs: every product's code, name, price and status badges, plus the
`totalCount` for the whole query. Reading that instead of rendering the page
means we can page to the very end and then *check* we got all of it — the old
browser scraper stopped after a fixed number of pages and could not tell when
the catalogue had outgrown them (SG did, at 103 items, in 2026-08).

Why we filter in the scraper instead of trusting the URL:
  `_f_productStatuses` misbehaves. Verified 2026-10: single values are fine
  (`=On` on AU correctly returns 0), but the combined `Waiting,On` is silently
  dropped whenever *neither* status has a match — AU, where everything has
  ended, gets its whole list back. SG only looks fine because it happens to
  have something on sale. So availability is decided here, from each
  product's own status, and the URL stays unfiltered.

Env vars:
  TELEGRAM_BOT_TOKEN   (required)
  TELEGRAM_CHAT_ID     (required) one or more chat ids, comma- or
                       newline-separated. Groups/channels are negative ids.
  WATCH_URLS           (required) newline- or comma-separated listing URLs.
                       No default: an unset value aborts the run instead of
                       quietly falling back to URLs baked into this file.
  STATE_FILE           (optional) default: state/seen.json
  MAX_PAGES            (optional) default: 50. A runaway guard, not a window:
                       a list longer than this fails loudly instead of being
                       silently cut short.
  REQUEST_DELAY        (optional) seconds between page fetches, default 0.5
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
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.5"))
ALERT_ON_ALL = os.environ.get("ALERT_ON_ALL", "") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# Dead man's switch (healthchecks.io ping URL). Optional — unset means off.
# This is the only thing that catches a *silent* stop: an expired PAT, a dead
# Cloudflare Worker, a runner that never started. Nothing inside this script
# can report those, because the script never runs. It also catches every site
# staying unreadable: such runs send no ping (see the end of this file).
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

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

Leave out _f_productStatuses — `Waiting,On` is silently ignored whenever
nothing matches it, and this script decides availability from each product's
own status anyway.

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
    # "End" is P-Bandai's own terminal sale status. Every ended product seen so
    # far also carries a CLOSED / OUT OF STOCK badge, so this changes nothing
    # today; it is here for the day one ships without a badge, which the
    # deny-list below would otherwise wave through as orderable.
    if item.get("sale_status") == "End":
        return False
    blob = " ".join(item.get("flags") or []).upper()
    return not any(mark in blob for mark in UNAVAILABLE_MARKERS)


def region_of(url: str) -> str:
    """https://p-bandai.com/sg/series/... -> 'sg'"""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    return parts[0].lower() if parts else "xx"


# --------------------------------------------------------------------------- #
# Fetching and parsing
# --------------------------------------------------------------------------- #

PRELOAD = re.compile(r"PRELOAD_DATA\s*=\s*")

CURRENCY_SYMBOL = {"SGD": "SG$", "AUD": "AU$", "HKD": "HK$", "TWD": "NT$",
                   "MYR": "RM", "USD": "US$"}


def fetch(url: str, attempts: int = 3) -> str | None:
    """GET a page, or None once retries are spent."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-SG,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"     fetch attempt {i + 1} failed: {e}", file=sys.stderr)
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    return None


def parse_results(page: str) -> tuple[list[dict], int] | None:
    """(products, totalCount) from the page's PRELOAD_DATA, or None if the page
    does not carry one — which is how a block page, a maintenance page or a
    layout change shows up, and must never be read as an empty list."""
    m = PRELOAD.search(page)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(page, m.end())
        pr = data["searchResult"]["productResults"]
    except (ValueError, KeyError, TypeError):
        return None
    products, total = pr.get("products"), pr.get("totalCount")
    if not isinstance(products, list) or not isinstance(total, int):
        return None
    return products, total


def to_item(p: dict, region: str) -> dict | None:
    """One PRELOAD_DATA product -> the item shape the rest of this file uses.

    `productCode` is the id in the product URL, so keys match what the old
    browser scraper stored and an upgrade does not re-announce everything.
    Flags come from the English badge labels for the same reason: they are the
    exact strings the cards print (checked against 124 stored records, 0 diffs).
    """
    code = p.get("productCode")
    if not code:
        return None

    names = p.get("productName") or {}
    title = names.get("en") or next((v for v in names.values() if v), "") or code

    flags = [((f.get("labelName") or {}).get("en") or "").strip()
             for f in (p.get("productFlags") or [])]
    flags = [f for f in flags if f]
    if not flags:
        # Raw codes ("PRE_ORDER_CLOSED") still trip the deny-list on CLOSED etc.
        flags = [c.replace("_", " ") for c in (p.get("flags") or []) if c]

    price = ""
    lp = p.get("fixedListPrice") or p.get("baseListPrice")
    if isinstance(lp, dict) and isinstance(lp.get("amount"), (int, float)):
        cur = lp.get("currency") or ""
        price = f"{CURRENCY_SYMBOL.get(cur, cur + ' ')}{lp['amount']:,.2f}"

    return {
        "id": code,
        "key": f"{region}:{code}",
        "region": region,
        "title": title.strip()[:200],
        "url": f"https://p-bandai.com/{region}/item/{code}",
        "price": price,
        "flags": flags,
        "sale_status": p.get("saleStatus"),
    }


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


def scrape_url(url: str) -> tuple[list[dict], bool]:
    """Read one listing URL to the end. Returns (items, ok).

    ok means *complete*, not merely "something came back": the number of
    distinct products read must equal the totalCount the server reported. A
    short read is the dangerous case — whatever it missed would be marked as
    gone, and announced as a restock the next time it reappeared.
    """
    limit = page_limit(url)
    region = region_of(url)
    found: dict[str, dict] = {}
    total = None

    for page_idx in range(MAX_PAGES):
        offset = page_idx * limit
        if total is not None and offset >= total:
            break
        if page_idx:
            time.sleep(REQUEST_DELAY)

        page = fetch(set_offset(url, offset))
        parsed = parse_results(page) if page is not None else None
        if parsed is None:
            print(f"     offset={offset}: no PRELOAD_DATA — treating as failure",
                  file=sys.stderr)
            return list(found.values()), False

        products, page_total = parsed
        if total is None:
            total = page_total
        elif page_total != total:
            # The catalogue changed under us; offsets have shifted, so this
            # read is not trustworthy. Next run will get a clean one.
            print(f"     totalCount moved {total} -> {page_total} mid-read",
                  file=sys.stderr)
            return list(found.values()), False

        page_items = [it for p in products if (it := to_item(p, region))]
        for it in page_items:
            found.setdefault(it["key"], it)

        avail = sum(1 for it in page_items if is_available(it))
        print(f"  -> [{region}] offset={offset} items={len(products)} "
              f"available={avail} total={total}", flush=True)

        if not products:
            break

    if not total:
        # A series page always lists *something* — P-Bandai keeps ended
        # products on it. Zero is a broken response, not an empty shelf.
        print(f"     [{region}] totalCount=0 — treating as failure",
              file=sys.stderr)
        return [], False

    if len(found) != total:
        hint = (f" (MAX_PAGES={MAX_PAGES} × limit={limit} is too small)"
                if total > MAX_PAGES * limit else "")
        print(f"     [{region}] read {len(found)} of {total}{hint}",
              file=sys.stderr)
        return list(found.values()), False

    return list(found.values()), True


def scrape_all() -> tuple[list[dict], dict[str, bool]]:
    """Returns (items, ok_by_region).

    Per region, not one global flag: one storefront going dark must not stop
    the others from being compared and alerted on. A region counts as ok only
    if *every* one of its URLs read cleanly — `present` is recomputed from the
    full region, so a half-read region would look like its missing half went
    away, which reads as a restock the next time it comes back.
    """
    all_items: dict[str, dict] = {}
    ok_by_region: dict[str, bool] = {}
    for url in WATCH_URLS:
        region = region_of(url)
        print(f"[scrape] {url}", flush=True)
        try:
            items, ok = scrape_url(url)
            for it in items:
                all_items.setdefault(it["key"], it)
        except Exception as e:
            ok = False
            print(f"  !! failed: {e}", file=sys.stderr)
        ok_by_region[region] = ok_by_region.get(region, True) and ok
    return list(all_items.values()), ok_by_region


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"items": {}, "initialized": False, "updated_at": None,
                "schema": 3}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        print("state file unreadable, starting fresh", file=sys.stderr)
        return {"items": {}, "initialized": False, "updated_at": None,
                "schema": 3}

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

    # Migration: v2 recorded every product it scraped, orderable or not. Once
    # P-Bandai SG started listing its whole back catalogue of closed
    # pre-orders (2026-08-26) that was 120-odd records nobody would ever be
    # alerted on. v3 only tracks products that have been orderable, so drop
    # everything that is not orderable now. Cost: if one of those ever
    # reopens it is announced as new rather than as a restock.
    if state.get("schema", 2) < 3:
        before = len(state.get("items", {}))
        state["items"] = {k: v for k, v in state.get("items", {}).items()
                          if v.get("present")}
        state["schema"] = 3
        print(f"schema v3: dropped {before - len(state['items'])} records that "
              f"are not orderable, kept {len(state['items'])}", flush=True)
    return state


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
# Dead man's switch
# --------------------------------------------------------------------------- #

def hc_ping(suffix: str = "", body: str = "") -> None:
    """Ping healthchecks.io. Silence is the alert, so this must never raise and
    never change the exit code — a failed ping is strictly less bad than a run
    that fails *because* the ping failed."""
    if not HEALTHCHECK_URL or DRY_RUN:
        return
    url = HEALTHCHECK_URL.rstrip("/") + suffix
    try:
        # No data -> GET. healthchecks caps the body at 100 KB; keep it small.
        req = urllib.request.Request(url, data=body.encode()[:10000] or None)
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"healthcheck ping failed ({suffix or '/'}): {e}", file=sys.stderr)


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

    items, ok_by_region = scrape_all()
    good = {r for r, ok in ok_by_region.items() if ok}
    bad = sorted(r for r, ok in ok_by_region.items() if not ok)

    # Whatever a failed region did return is partial. Drop it — letting it
    # through would make the items it missed look like they went away.
    items = [it for it in items if it["region"] in good]
    available = [it for it in items if ALERT_ON_ALL or is_available(it)]
    print(f"[scrape] ok={sorted(good) or '-'} failed={bad or '-'} "
          f"scraped={len(items)} alertable={len(available)}", flush=True)

    if not good:
        # Every region dark: layout change, bot block, or network trouble.
        # Bail without touching state so the next good run doesn't report the
        # whole catalogue as new.
        print("no region scraped cleanly — state untouched", file=sys.stderr)
        # Annotates the run's summary page; the run itself stays green.
        print("::warning::nothing could be read this run — state untouched",
              flush=True)
        return UNREADABLE

    before = on_disk_fingerprint()
    state = load_state()
    known: dict = state.get("items", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items, back_items = [], []
    alertable_keys = {it["key"] for it in available}

    # Track what has been orderable at some point. Something that has never
    # been orderable is not recorded at all — P-Bandai lists its entire back
    # catalogue of closed pre-orders, and storing those is what grew the state
    # file past 1500 lines. Once recorded, a product stays, with `present`
    # following its orderability, so sold-out -> back-in-stock reads as a
    # restock.
    for it in items:
        key = it["key"]
        prev = known.get(key)
        now_alertable = key in alertable_keys

        if prev is None and not now_alertable:
            continue

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
        # Only a region we actually read can testify that an item is gone.
        # Records belonging to a failed region are left exactly as they were.
        region = rec.get("region") or key.split(":", 1)[0]
        if key not in scraped_keys and region in good:
            rec["present"] = False

    if not state.get("initialized"):
        tg_send(
            "✅ <b>P-Bandai restock bot 已启动</b>\n"
            f"监控中：{len(WATCH_URLS)} 个列表，共 {len(items)} 件商品，"
            f"其中现在可下单 {len(available)} 件。\n"
            + (f"⚠️ 读不到：{', '.join(r.upper() for r in bad)}\n" if bad else "")
            + "之后有上新或补货才会再通知你。"
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

    # A storefront going dark (or coming back) is worth one message per
    # transition, not one per hour. Anything that stays broken is the dead
    # man's switch's job, not Telegram's.
    prev_bad = set(state.get("failed_regions") or [])
    if state.get("initialized") and set(bad) != prev_bad:
        if bad:
            tg_send(
                "⚠️ <b>P-Bandai 抓取异常</b>\n"
                f"读不到商品列表：{', '.join(r.upper() for r in bad)}\n"
                f"其余 {len(good)} 个站照常监控中。异常站点的记录已冻结，"
                "不会误报上新或补货。"
            )
        else:
            tg_send(
                "✅ <b>P-Bandai 抓取已恢复</b>\n"
                f"{', '.join(r.upper() for r in sorted(prev_bad))} 恢复正常。"
            )

    state["items"] = known
    state["initialized"] = True
    state["failed_regions"] = bad
    if fingerprint(state) != before:
        save_state(state)
    else:
        print("nothing changed beyond timestamps — state file left as is",
              flush=True)

    print(f"[done] new={len(new_items)} back={len(back_items)} "
          f"tracked={len(known)} failed={bad or '-'}", flush=True)
    # Partial failure is not a run failure: the good regions were compared and
    # alerted on, and Telegram already said which region is dark. Failing here
    # would just turn the dead man's switch red every hour for no new reason.
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
