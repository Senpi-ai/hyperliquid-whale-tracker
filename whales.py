"""hyperliquid-whale-tracker, from Senpi (senpi.ai).

Prints every big trade on Hyperliquid as it happens, with what Senpi's proven traders hold in that market.

    python whales.py [--min-usd 1000000] [--assets BTC,ETH,xyz:TSLA | --top 20] [--watch 0xabc...,0xdef...] [--webhook URL]

Uses only Hyperliquid's public API and Senpi's free public asset JSON: no account, no API key.
"""
import argparse
import json
import re
import threading
import time
import urllib.request

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

INFO = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"


def request(url, body=None):
    """GET a URL, or POST JSON to it. senpi.ai refuses Python's default user agent, so send our own."""
    headers = {"User-Agent": "hyperliquid-whale-tracker"}
    if body is not None:
        body, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=20) as r:
        return r.read()


def info(body):
    return json.loads(request(INFO, body))


def markets():
    """24h dollar volume of every listed perp market on every perp dex (HIP-3 markets look like xyz:TSLA)."""
    volume = {}
    for dex in [""] + [d["name"] for d in info({"type": "perpDexs"})[1:]]:
        meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": dex})
        volume.update({u["name"]: float(c["dayNtlVlm"]) for u, c in zip(meta["universe"], ctxs) if not u.get("isDelisted")})
    return volume


def pick(volume, assets, top):
    """Hyperliquid drops the whole connection on an unknown market name, so check names before subscribing."""
    if not assets:
        return sorted(volume, key=volume.get, reverse=True)[:top]
    names = {m.lower(): m for m in volume}
    unknown = [a for a in assets if a.lower() not in names]
    if unknown:
        raise SystemExit(f"Not a listed Hyperliquid perp market: {', '.join(unknown)}. Names look like BTC, kPEPE, xyz:TSLA.")
    return [names[a.lower()] for a in assets]


def big_trades(trades, min_usd):
    """One taker order can fill against hundreds of resting orders, and each fill is its own print. Add the prints of
    each taker order back together (same block, transaction and side) and keep the orders worth at least min_usd.
    Groups within one websocket message, which held every order's prints in testing; buffer across messages if an
    order ever arrives split."""
    orders = {}
    for t in trades:
        buyer, seller = t["users"]
        taker = buyer if t["side"] == "B" else seller  # side is the taker's: B bought, A sold
        key = (t["coin"], t["time"], t["hash"], t["side"], taker)
        o = orders.setdefault(key, {"key": key, "asset": t["coin"], "side": t["side"], "time": t["time"], "taker": taker,
                                    "buyers": [], "sellers": [], "size": 0.0, "usd": 0.0})
        o["size"] += float(t["sz"])
        o["usd"] += float(t["sz"]) * float(t["px"])
        for group, address in ((o["buyers"], buyer), (o["sellers"], seller)):
            if address not in group:
                group.append(address)
    return [o for o in orders.values() if o["usd"] >= min_usd]


_context, _tracked = {}, [0.0, set()]


def senpi_context(asset):
    """What Senpi's proven traders hold in this market, from senpi.ai/assets/<symbol>/data.json, cached 5 minutes like
    the file. Just the market's link when no proven trader holds it (positioning is null)."""
    cached = _context.get(asset)
    if not cached or time.time() - cached[0] > 300:
        try:
            d = json.loads(request(f"https://senpi.ai/assets/{asset}/data.json"))
            p = d["positioning"]
            line = (f"Senpi proven traders: {money(p['long'])} long ({plural(p['longWallets'], 'wallet')}), "
                    f"{money(p['short'])} short ({plural(p['shortWallets'], 'wallet')}). {d['url']}"
                    if p is not None else f"Senpi market page: {d['url']}")
        except (OSError, ValueError, KeyError, TypeError):  # not every market has a Senpi page
            line = ""
        cached = _context[asset] = (time.time(), line)
    return cached[1]


def tracked(address):
    """Is this a wallet senpi.ai tracks (senpi.ai/traders.json, read hourly)? Only those get a link: a page for any
    other wallet makes senpi.ai read Hyperliquid live, and chat apps open every link in an alert to preview it."""
    if time.time() - _tracked[0] > 3600:
        try:
            _tracked[1] = {t["address"].lower() for t in json.loads(request("https://senpi.ai/traders.json"))["traders"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # keep the last list; with none yet, no taker gets a link until the next try
        _tracked[0] = time.time()
    return address.lower() in _tracked[1]


def trade_text(o, context, linked=False):
    side = "BUY" if o["side"] == "B" else "SELL"
    lines = [f"{clock(o['time'])}  {o['asset']}  {side} {num(o['size'])} @ {num(o['usd'] / o['size'])} = {money(o['usd'])}  "
             f"buyer {who(o['buyers'])}, seller {who(o['sellers'])}"]
    if linked:
        lines.append(f"  taker: https://senpi.ai/traders/{o['taker']}")
    return "\n".join(lines + ([f"  {context}"] if context else []))


def on_trades(trades, started, seen, min_usd, alert):
    """Alert each big order once. Each subscription starts with a replay of recent trades, so skip anything from before we started."""
    for o in big_trades(trades, min_usd):
        if o["time"] >= started and o["key"] not in seen:
            seen.add(o["key"])
            alert(trade_text(o, senpi_context(o["asset"]), tracked(o["taker"])))


def stream(assets, min_usd, alert):
    started, seen = time.time() * 1000, set()
    while True:
        try:
            with connect(WS, open_timeout=15) as ws:
                for asset in assets:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": asset}}))
                pinged = time.time()
                while True:
                    try:
                        msg = json.loads(ws.recv(timeout=10))
                    except TimeoutError:
                        msg = {}
                    if msg.get("channel") == "trades":
                        on_trades(msg["data"], started, seen, min_usd, alert)
                    if time.time() - pinged > 30:  # Hyperliquid closes connections that go 60 seconds without a message
                        ws.send(json.dumps({"method": "ping"}))
                        pinged = time.time()
        except (OSError, ValueError, WebSocketException) as e:  # ValueError: a frame that is not JSON
            print(f"Connection lost ({e}), reconnecting in 5 seconds", flush=True)
            time.sleep(5)


def positions(user, dexes):
    """{asset: (signed size, dollar value)} for one wallet across every perp dex."""
    held = {}
    for dex in dexes:
        for p in info({"type": "clearinghouseState", "user": user, "dex": dex})["assetPositions"]:
            held[p["position"]["coin"]] = (float(p["position"]["szi"]), float(p["position"]["positionValue"]))
    return held


def changes(old, new, min_usd):
    """One plain line for each position that opened, closed, flipped or changed by at least min_usd."""
    lines = []
    for asset in sorted(set(old) | set(new)):
        (was, was_usd), (now, now_usd) = old.get(asset, (0.0, 0.0)), new.get(asset, (0.0, 0.0))
        price = now_usd / abs(now) if now else was_usd / abs(was) if was else 0.0
        moved = abs(now - was) * price  # dollars of size bought or sold since the last look
        if now == was or moved < min_usd:
            continue
        side = "long" if (now or was) > 0 else "short"
        if not was:
            lines.append(f"opened a {money(now_usd)} {asset} {side}")
        elif not now:
            lines.append(f"closed a {money(was_usd)} {asset} {side}")
        elif was * now < 0:
            lines.append(f"flipped {asset} to {side}, now {money(now_usd)}")
        else:
            verb = "added {} to" if abs(now) > abs(was) else "cut {} from"
            lines.append(f"{verb.format(money(moved))} the {asset} {side}, now {money(now_usd)}")
    return lines


def poll(users, dexes, last, min_usd, alert):
    for user in users:
        try:
            held = positions(user, dexes)
        except (OSError, ValueError, KeyError, TypeError) as e:  # KeyError: a reply without assetPositions
            print(f"Could not read {short(user)}: {e!r}", flush=True)
            continue
        if user in last:
            for line in changes(last[user], held, min_usd):
                alert(f"{clock(time.time() * 1000)}  {short(user)} {line}\n  https://senpi.ai/traders/{user}")
        else:
            print(f"Watching {short(user)}: {plural(len(held), 'open position')}, {money(sum(v for _, v in held.values()))}", flush=True)
        last[user] = held


def watch(users, dexes, min_usd, alert):
    last = {}
    while True:
        poll(users, dexes, last, min_usd, alert)
        time.sleep(60)


def alerter(webhook):
    def alert(text):
        print(text, flush=True)
        if webhook:
            try:
                request(webhook, {"text": text})  # Slack incoming webhooks, and Discord's at .../slack
            except OSError as e:
                print(f"  webhook failed: {e}", flush=True)
    return alert


def money(x):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= size:
            return f"${x / size:.1f}{unit}"
    return f"${x:,.0f}"


def num(x):
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:.4g}"


def plural(n, word):
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def clock(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ms / 1000))


def short(address):
    return "0x" + address[-5:]


def who(addresses):
    return short(addresses[0]) + (f" +{len(addresses) - 1} more" if len(addresses) > 1 else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print every big trade on Hyperliquid as it happens.")
    ap.add_argument("--min-usd", type=float, default=1_000_000, help="smallest order or position change to show, in dollars (default 1000000)")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--assets", help="markets as Hyperliquid names them, comma separated: BTC,ETH,kPEPE,xyz:TSLA")
    which.add_argument("--top", type=int, default=20, help="follow the N markets with the most 24h volume (default 20)")
    ap.add_argument("--watch", help="wallet addresses to check every 60 seconds, comma separated")
    ap.add_argument("--webhook", help="Slack incoming webhook URL, or a Discord webhook URL with /slack added")
    args = ap.parse_args(argv)
    users = [u.strip().lower() for u in (args.watch or "").split(",") if u.strip()]
    bad = [u for u in users if not re.fullmatch(r"0x[0-9a-f]{40}", u)]
    if bad:
        ap.error(f"not a wallet address: {', '.join(bad)}")
    try:
        volume = markets()
    except OSError as e:
        raise SystemExit(f"Could not reach Hyperliquid's API: {e}")
    assets = pick(volume, [a.strip() for a in args.assets.split(",") if a.strip()] if args.assets else None, args.top)
    alert = alerter(args.webhook)
    print(f"Showing orders of {money(args.min_usd)} or more in {plural(len(assets), 'market')}: {', '.join(assets)}")
    print("Free live view of where top traders and the biggest whales are positioned: https://senpi.ai/traders", flush=True)
    if users:
        dexes = [""] + sorted({m.split(":")[0] for m in volume if ":" in m})
        threading.Thread(target=watch, args=(users, dexes, args.min_usd, alert), daemon=True).start()
    try:
        stream(assets, args.min_usd, alert)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
