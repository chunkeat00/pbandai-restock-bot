#!/usr/bin/env python3
"""
Kelab Gasing Beyblade restock bot.

Watches one or more collection pages on kelabgasingbeyblade.my and sends a
Telegram message when something new appears or comes back.

The site drops sold-out products from its collection pages, so presence on the
page *is* availability — no need to open each product. One page, one request,
one comparison. That also means a page we cannot read must never be mistaken
for an empty shelf; see scrape_group().

Pages are server-rendered, so this is stdlib-only: no Playwright, no chromium,
no pip install.
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

DEFAULT_URLS = "https://www.kelabgasingbeyblade.my/beyblade-x"
# `${{ vars.X }}` on an unset repo variable expands to an empty string, not to
# nothing — so an unset variable must fall back the same way a missing one does.
WATCH_URLS = [
    u.strip() for u in re.split(
        r"[\n,]+", os.environ.get("KGB_WATCH_URLS", "").strip() or DEFAULT_URLS)
    if u.strip() and not u.strip().startswith("#")
]

STATE_FILE = Path(os.environ.get("STATE_FILE", "kgb-restock-bot/state/seen.json"))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# Dead man's switch (healthchecks.io ping URL). Optional — unset means off.
# The only thing that catches a *silent* stop: expired PAT, dead trigger,
# runner that never started. Nothing in here can report those.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

# robots.txt allows User-agent: * on everything but /admin. The AI-crawler
# blocks (ClaudeBot, GPTBot, CCBot, ...) are a different thing and are not what
# this is. Site sits behind Cloudflare, which is unfriendly to novelty agents,
# so present as an ordinary browser.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def fetch(url: str, attempts: int = 3) -> str | None:
    """GET a page, or None once retries are spent."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-MY,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"     fetch attempt {i + 1} failed: {e}", file=sys.stderr)
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

# Each card is one <a> wrapping the whole tile. Non-greedy up to </a> keeps a
# card's spans from bleeding into the next card's.
CARD = re.compile(
    r'<a\s+href="(https://(?:www\.)?kelabgasingbeyblade\.my/products/[^"#?]+)"'
    r'[^>]*>(.*?)</a>',
    re.S,
)
TITLE = re.compile(r'product-title[^>]*>([^<]*)<')
DESC = re.compile(r'product-desc[^>]*>([^<]*)<')


def group_of(url: str) -> str:
    """Collection slug, e.g. .../beyblade-x -> "beyblade-x". Namespaces state
    keys so two collections cannot collide on the same product."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    return parts[-1].lower() if parts else "root"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


def parse_listing(page: str, group: str) -> list[dict]:
    """Every product tile on a collection page, in page order, de-duplicated.

    A tile carries the product name, a bracketed category ("[ Starter ]") and a
    price ("MYR 79.90") in sibling spans that share one class, so they are told
    apart by shape rather than by position.
    """
    out: list[dict] = []
    seen: set[str] = set()

    for url, body in CARD.findall(page):
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug in seen:
            continue
        seen.add(slug)

        t = TITLE.search(body)
        descs = [clean(d) for d in DESC.findall(body)]
        category = next((d for d in descs if d.startswith("[")), "")
        price = next((d for d in descs if re.search(r"\d", d) and not d.startswith("[")), "")

        out.append({
            "key": f"{group}:{slug}",
            "group": group,
            "slug": slug,
            "title": clean(t.group(1)) if t else slug,
            "url": url,
            "price": price,
            "category": category,
        })
    return out


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

def scrape_group(url: str) -> tuple[list[dict], bool]:
    """Scrape one collection page. Returns (items, ok)."""
    group = group_of(url)
    print(f"[scrape] {url}", flush=True)

    page = fetch(url)
    if page is None:
        print("     collection page unreachable", file=sys.stderr)
        return [], False

    items = parse_listing(page, group)
    if not items:
        # Sold-out products leave the page, but the page never empties itself
        # of markup. Zero tiles means a layout change, a block, or the site's
        # queue interstitial — "we cannot see the shelf", never "the shelf is
        # empty". Treating this as ok would delist the entire catalogue and
        # then re-announce it as a restock the moment the page came back.
        print("     no product tiles found — treating as failure",
              file=sys.stderr)
        return [], False

    print(f"     found={len(items)}", flush=True)
    return items, True


def scrape_all() -> tuple[list[dict], dict[str, bool]]:
    all_items: list[dict] = []
    ok_by_group: dict[str, bool] = {}
    for url in WATCH_URLS:
        group = group_of(url)
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
    meta = " · ".join(x for x in (it.get("category"), it.get("price")) if x)
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
    bad_urls = [u for u in WATCH_URLS if not u.startswith(("http://", "https://"))]
    if not WATCH_URLS or bad_urls:
        print(f"KGB_WATCH_URLS invalid: {bad_urls or 'empty'}", file=sys.stderr)
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

    # Whatever a failed collection returned is partial — drop it, or the
    # products it missed would look like they sold out.
    items = [it for it in items if it["group"] in good]
    print(f"[scrape] ok={sorted(good) or '-'} failed={bad or '-'} "
          f"listed={len(items)}", flush=True)

    if not good:
        print("no collection scraped cleanly — state untouched", file=sys.stderr)
        return 1

    state = load_state()
    known: dict = state.get("items", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_items, back_items = [], []

    # On this site the listing is the stock signal: if it is on the page, it is
    # buyable. So every parsed item is `present`, and the sweep below is what
    # marks a sell-out.
    for it in items:
        prev = known.get(it["key"])
        if prev is None:
            new_items.append(it)
        elif not prev.get("present", False):
            back_items.append(it)

        known[it["key"]] = {
            "group": it["group"],
            "title": it["title"],
            "url": it["url"],
            "price": it.get("price", ""),
            "category": it.get("category", ""),
            "present": True,
            "first_seen": (prev or {}).get("first_seen", now),
            "last_seen": now,
        }

    # Gone from a collection we read cleanly = sold out. Records in a failed
    # group keep whatever `present` they had.
    listed_keys = {it["key"] for it in items}
    for key, rec in known.items():
        group = rec.get("group") or key.split(":", 1)[0]
        if group in good and key not in listed_keys:
            rec["present"] = False

    if not state.get("initialized"):
        tg_send(
            "✅ <b>Kelab Gasing Beyblade restock bot 已启动</b>\n"
            f"监控中：{len(WATCH_URLS)} 个分类，现在有货 {len(items)} 件。\n"
            + (f"⚠️ 读不到：{', '.join(bad)}\n" if bad else "")
            + "之后有上新或补货才会再通知你。"
        )
    else:
        if new_items:
            send_batched(f"🆕 <b>Beyblade 上新 {len(new_items)} 件</b>",
                         [fmt_item(i, "🔹") for i in new_items])
        if back_items:
            send_batched(f"♻️ <b>Beyblade 补货 {len(back_items)} 件</b>",
                         [fmt_item(i, "🔸") for i in back_items])
        if not new_items and not back_items:
            print("no changes", flush=True)

    prev_bad = set(state.get("failed_groups") or [])
    if state.get("initialized") and set(bad) != prev_bad:
        if bad:
            tg_send(
                "⚠️ <b>Beyblade 抓取异常</b>\n"
                f"读不到分类页：{', '.join(bad)}\n"
                "该分类的记录已冻结，不会误报上新或补货。"
            )
        else:
            tg_send(f"✅ <b>Beyblade 抓取已恢复</b>\n{', '.join(sorted(prev_bad))} 恢复正常。")

    state["items"] = known
    state["initialized"] = True
    state["failed_groups"] = bad
    save_state(state)

    print(f"[done] new={len(new_items)} back={len(back_items)} "
          f"tracked={len(known)} failed={bad or '-'}", flush=True)
    return 0


if __name__ == "__main__":
    hc_ping("/start")
    try:
        code = main()
    except BaseException:
        hc_ping("/fail", traceback.format_exc())
        raise
    hc_ping("" if code == 0 else "/fail", f"exit={code}")
    sys.exit(code)
