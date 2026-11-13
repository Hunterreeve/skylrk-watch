#!/usr/bin/env python3
"""
SKYLRK stock watcher (works for any Shopify store).

Checks https://skylrk.com/products.json and sends a push notification when:
  - a new product or variant appears (new drop)
  - a sold-out variant comes back in stock (restock)
  - a price changes
Optionally also when something sells out (set NOTIFY_SOLD_OUT=1).

Point it at another Shopify store with STORE / STORE_NAME / STATE_FILE.
If that store is behind Shopify's "Enter store using password" page, the
watcher just remembers it is locked and sends an alert the moment it opens.

Notifications go to ntfy.sh (free phone push app) and/or a Discord webhook.
Uses only the Python standard library — nothing to install.

Usage:
  python skylrk_watch.py            # check once (good for cron / GitHub Actions)
  python skylrk_watch.py --loop     # check every 15 minutes forever
  python skylrk_watch.py --test     # send a test notification

Environment variables:
  NTFY_TOPIC        your private ntfy topic name (e.g. skylrk-hunter-8f3k2)
  DISCORD_WEBHOOK   optional Discord webhook URL
  NOTIFY_SOLD_OUT   set to 1 to also get sold-out alerts
  STATE_FILE        where to remember the last check (default: state.json)
  STORE             store address (default: https://skylrk.com)
  STORE_NAME        name used in notifications (default: SKYLRK)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STORE = (os.environ.get("STORE", "").strip() or "https://skylrk.com").rstrip("/")
STORE_NAME = os.environ.get("STORE_NAME", "").strip() or "SKYLRK"
INTERVAL_SECONDS = 15 * 60
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
NOTIFY_SOLD_OUT = os.environ.get("NOTIFY_SOLD_OUT", "0") == "1"
HEADERS = {"User-Agent": "Mozilla/5.0 (skylrk-stock-watcher; personal use)"}


LOCKED_STATE = {"_locked": True}


class StoreLocked(Exception):
    """The store is behind Shopify's password page."""


def http_get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        # A password-protected Shopify store answers 401 Unauthorized.
        if e.code == 401:
            raise StoreLocked() from None
        raise
    with resp:
        # Some locked stores redirect to /password with a normal 200 instead.
        final_path = urllib.parse.urlparse(resp.geturl()).path.rstrip("/")
        if final_path.endswith("/password"):
            raise StoreLocked()
        return json.loads(resp.read().decode("utf-8"))


def fetch_inventory():
    """Return {variant_id: {...}} for every variant in the store.

    Raises StoreLocked if the store is behind the password page.
    """
    inventory = {}
    page = 1
    while True:
        data = http_get_json(f"{STORE}/products.json?limit=250&page={page}")
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            for v in p.get("variants", []):
                vt = v.get("title") or ""
                name = p["title"] if vt in ("", "Default Title") else f"{p['title']} – {vt}"
                inventory[str(v["id"])] = {
                    "name": name,
                    "available": bool(v.get("available")),
                    "price": v.get("price"),
                    "url": f"{STORE}/products/{p['handle']}?variant={v['id']}",
                }
        page += 1
        if page > 20:  # safety stop
            break
    return inventory


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(inventory):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=1, sort_keys=True)


def diff(old, new):
    """Return a list of (kind, item, detail) changes."""
    changes = []
    for vid, item in new.items():
        prev = old.get(vid)
        if prev is None:
            changes.append(("NEW", item, "in stock" if item["available"] else "listed (sold out)"))
            continue
        if item["available"] and not prev["available"]:
            changes.append(("RESTOCK", item, f"${item['price']}"))
        elif NOTIFY_SOLD_OUT and prev["available"] and not item["available"]:
            changes.append(("SOLD OUT", item, ""))
        if item["price"] != prev.get("price"):
            changes.append(("PRICE", item, f"${prev.get('price')} → ${item['price']}"))
    return changes


def notify(title, message, click_url=None):
    sent = False
    if NTFY_TOPIC:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "ignore").decode(),
                "Tags": "shopping_cart",
                "Priority": "high",
                **({"Click": click_url} if click_url else {}),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30).close()
        sent = True
    if DISCORD_WEBHOOK:
        body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=body,
            headers={"Content-Type": "application/json", **HEADERS}, method="POST",
        )
        urllib.request.urlopen(req, timeout=30).close()
        sent = True
    if not sent:
        print(f"[no notifier configured] {title}: {message}")


def check_once():
    old = load_state()
    try:
        new = fetch_inventory()
    except StoreLocked:
        if old is not None and old != LOCKED_STATE:
            notify(f"{STORE_NAME} locked", "The store went back behind its password page.", STORE)
        save_state(LOCKED_STATE)
        print(time.strftime("%Y-%m-%d %H:%M"), f"{STORE_NAME} is locked (password page)")
        return

    if old == LOCKED_STATE:
        save_state(new)
        in_stock = [i for i in new.values() if i["available"]]
        lines = [f"{i['name']} ${i['price']}" for i in in_stock]
        message = f"The password page is down. {len(new)} variants listed, {len(in_stock)} in stock."
        if lines:
            message += "\n" + "\n".join(lines[:12])
            if len(lines) > 12:
                message += f"\n…and {len(lines) - 12} more"
        print(message)
        notify(f"{STORE_NAME} is OPEN", message, STORE)
        return

    if old is None:
        save_state(new)
        in_stock = sum(1 for i in new.values() if i["available"])
        print(f"Baseline saved: {len(new)} variants, {in_stock} in stock. Alerts start next check.")
        return

    changes = diff(old, new)
    save_state(new)
    if not changes:
        print(time.strftime("%Y-%m-%d %H:%M"), "no changes")
        return

    lines = [f"{kind}: {item['name']} {detail}".strip() for kind, item, detail in changes]
    print("\n".join(lines))
    kinds = sorted({k for k, _, _ in changes})
    title = f"{STORE_NAME} update: {', '.join(kinds)}"
    message = "\n".join(lines[:15]) + (f"\n…and {len(lines) - 15} more" if len(lines) > 15 else "")
    click = changes[0][1]["url"] if len(changes) == 1 else STORE
    notify(title, message, click)


def main():
    if "--test" in sys.argv:
        notify(f"{STORE_NAME} watcher test", "Notifications are working.", STORE)
        print("Test notification sent.")
        return
    if "--loop" in sys.argv:
        while True:
            try:
                check_once()
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                print("Check failed, will retry:", e)
            time.sleep(INTERVAL_SECONDS)
    else:
        check_once()


if __name__ == "__main__":
    main()
