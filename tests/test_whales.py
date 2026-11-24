"""Offline tests. The fixtures are real Hyperliquid and Senpi responses with every wallet address, transaction hash
and trade id replaced by made-up ones."""
import http.server
import json
import re
import sys
import threading
import urllib.error
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import whales  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
USER = "0x00000000000000000000000000000000000000aa"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def senpi(url, body=None):
    """senpi.ai as the fixtures have it: the tracked-wallet list, BTC with proven traders, POLYX with none."""
    name = "senpi_traders" if url.endswith("/traders.json") else "senpi_" + url.split("/assets/")[1].split("/")[0]
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return path.read_bytes()


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    """Every test starts with empty caches and never reaches senpi.ai unless it stubs request."""
    whales._context.clear()
    monkeypatch.setattr(whales, "_tracked", [float("inf"), set()])


def notional(trades):
    return sum(float(t["sz"]) * float(t["px"]) for t in trades)


def fake_info(body):
    if body["type"] == "metaAndAssetCtxs":
        return load("metaAndAssetCtxs" + (f"_{body['dex']}" if body["dex"] else ""))
    if body["type"] == "clearinghouseState":
        return load("clearinghouseState") if body["dex"] == "" else {"assetPositions": []}
    return load(body["type"])


def test_one_taker_order_filled_as_many_prints_is_one_trade():
    trades = load("trades_live")["data"]
    assert len(trades) == 6 and max(notional([t]) for t in trades) < 100_000
    (order,) = whales.big_trades(trades, 100_000)
    assert (order["asset"], order["side"], order["usd"]) == ("ETH", "A", pytest.approx(notional(trades)))
    assert order["sellers"] == [order["taker"]] and len(order["buyers"]) == len({t["users"][0] for t in trades})


def test_prints_from_different_orders_stay_apart():
    trades = load("trades_mixed")["data"]
    orders = whales.big_trades(trades, 0)
    assert len(orders) == 5 and sum(o["usd"] for o in orders) == pytest.approx(notional(trades))


def test_replayed_and_repeated_trades_alert_once(monkeypatch):
    monkeypatch.setattr(whales, "senpi_context", lambda asset: "")
    replay, live = load("trades_snapshot")["data"], load("trades_live")["data"]
    started = live[0]["time"]
    assert whales.big_trades(replay, 1_000_000) and max(t["time"] for t in replay) < started  # a $1M+ order from before we started
    alerts, seen = [], set()
    for trades in (replay, live, live):
        whales.on_trades(trades, started, seen, 100_000, alerts.append)
    assert len(alerts) == 1 and "  ETH  SELL " in alerts[0]


def test_alert_text(monkeypatch):
    monkeypatch.setattr(whales, "request", senpi)
    (order,) = whales.big_trades(load("trades_snapshot")["data"], 1_000_000)
    first, link, context = whales.trade_text(order, whales.senpi_context("BTC"), True).splitlines()
    assert re.search(r"  BTC  (BUY|SELL) \S+ @ \S+ = \$1\.1M  buyer 0x[0-9a-f]{5}", first)
    assert all(len(a) == 7 for a in re.findall(r"0x[0-9a-f]+", first))  # short addresses only
    assert link == f"  taker: https://senpi.ai/traders/{order['taker']}"
    assert context == "  Senpi proven traders: $139.2M long (29 wallets), $309.6M short (46 wallets). https://senpi.ai/assets/BTC"
    text = first + link + context
    assert "coin" not in text.lower() and not re.search("[%s%s]" % (chr(0x2013), chr(0x2014)), text)


def test_a_market_no_proven_trader_holds_gets_just_its_link(monkeypatch):
    monkeypatch.setattr(whales, "request", senpi)
    assert load("senpi_POLYX")["positioning"] is None                   # real response, 24 Nov 2026
    assert whales.senpi_context("POLYX") == "Senpi market page: https://senpi.ai/assets/POLYX"
    alerts, trades = [], [dict(t, coin="POLYX") for t in load("trades_snapshot")["data"]]
    whales.on_trades(trades, 0, set(), 1_000_000, alerts.append)      # used to raise TypeError and end the script
    assert len(alerts) == 1 and alerts[0].endswith("  Senpi market page: https://senpi.ai/assets/POLYX")
    assert "taker:" not in alerts[0]                                   # this taker is not a tracked wallet


def test_only_a_wallet_senpi_tracks_gets_a_taker_link(monkeypatch):
    monkeypatch.setattr(whales, "request", senpi)
    whales._tracked[0] = 0.0                                            # due for a read
    assert whales.tracked("0x0000000000000000000000000000000000000002")
    assert not whales.tracked("0x0000000000000000000000000000000000000003")
    (order,) = whales.big_trades(load("trades_snapshot")["data"], 1_000_000)
    assert "taker:" not in whales.trade_text(order, "", False)


def test_an_unreadable_wallet_list_links_nobody_and_does_not_crash(monkeypatch):
    def down(url, body=None):
        raise urllib.error.URLError("senpi.ai unreachable")
    monkeypatch.setattr(whales, "request", down)
    whales._tracked[0] = 0.0
    assert not whales.tracked("0x0000000000000000000000000000000000000002")


def test_no_senpi_line_for_a_market_without_a_page(monkeypatch):
    def missing(url, body=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(whales, "request", missing)
    whales._context.clear()
    assert whales.senpi_context("xyz:NOPE") == ""


def test_markets_cover_every_dex_and_names_are_checked(monkeypatch):
    monkeypatch.setattr(whales, "info", fake_info)
    volume = whales.markets()
    assert {"BTC", "xyz:NVDA"} <= set(volume) and "MATIC" not in volume  # delisted
    assert whales.pick(volume, None, 3) == sorted(volume, key=volume.get, reverse=True)[:3]
    assert whales.pick(volume, ["btc", "XYZ:nvda"], 20) == ["BTC", "xyz:NVDA"]
    with pytest.raises(SystemExit):
        whales.pick(volume, ["NOTREAL"], 20)


def test_position_changes_of_at_least_the_threshold():
    old = {"BTC": (2.0, 120_000.0), "ETH": (10.0, 20_000.0), "SOL": (-100.0, 7_000.0), "XRP": (-10.0, 20.0)}
    new = {"BTC": (30.0, 1_800_000.0), "ETH": (-5.0, 10_000.0), "HYPE": (1000.0, 30_000.0), "XRP": (5.0, 10.0)}
    assert whales.changes(old, new, 25_000) == [
        "added $1.7M to the BTC long, now $1.8M",
        "flipped ETH to short, now $10.0K",  # $30K of size sold: 10 long to 5 short
        "opened a $30.0K HYPE long",
    ]  # the $7K SOL close and the $30 XRP flip are under $25K
    assert whales.changes(old, new, 0)[-1] == "flipped XRP to long, now $10"
    assert whales.changes(new, dict(new, BTC=(30.1, 1_806_000.0)), 25_000) == []


def test_a_wallet_reply_without_positions_does_not_stop_the_watch(monkeypatch, capsys):
    monkeypatch.setattr(whales, "info", lambda body: {"marginSummary": {}})   # no assetPositions
    alerts, last = [], {}
    whales.poll([USER], [""], last, 1_000_000, alerts.append)
    assert alerts == [] and last == {} and "Could not read 0x000aa" in capsys.readouterr().out


def test_watch_takes_a_baseline_then_alerts(monkeypatch):
    monkeypatch.setattr(whales, "info", fake_info)
    alerts, last = [], {}
    whales.poll([USER], ["", "xyz"], last, 1_000_000, alerts.append)
    assert set(last[USER]) == {"BTC", "HYPE"} and alerts == []
    last[USER] = {}
    whales.poll([USER], ["", "xyz"], last, 1_000_000, alerts.append)
    assert len(alerts) == 1 and "0x000aa opened a $1.5M BTC long" in alerts[0]  # the $65K HYPE short is under $1M
    assert alerts[0].endswith(f"https://senpi.ai/traders/{USER}")


class FakeSocket:
    """Plays back frames, then raises KeyboardInterrupt, the way Ctrl+C ends the script."""

    def __init__(self, *frames):
        self.frames, self.sent = list(frames), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.frames:
            raise KeyboardInterrupt
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)


def test_reconnects_after_a_drop(monkeypatch):
    live = load("trades_live")
    sockets, opened = [FakeSocket(ConnectionClosedError(None, None)), FakeSocket(live, live)], []

    def connect(url, open_timeout):
        opened.append(sockets[len(opened)])
        return opened[-1]

    monkeypatch.setattr(whales, "connect", connect)
    monkeypatch.setattr(whales, "senpi_context", lambda asset: "")
    monkeypatch.setattr(whales.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(whales.time, "time", lambda: live["data"][0]["time"] / 1000)
    alerts = []
    with pytest.raises(KeyboardInterrupt):
        whales.stream(["ETH", "BTC"], 100_000, alerts.append)
    assert len(opened) == 2 and [len(s.sent) for s in sockets] == [2, 2]  # subscribed again after the drop
    assert len(alerts) == 1


def test_webhook_gets_json_with_a_text_field():
    got = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.handle_request, daemon=True).start()
    whales.alerter(f"http://127.0.0.1:{server.server_port}/hook")("big trade")
    server.server_close()
    assert got == [{"text": "big trade"}]


def test_rejects_a_bad_watch_address():
    with pytest.raises(SystemExit):
        whales.main(["--watch", "0x1234"])


def readme_example(monkeypatch):
    """The alerts the README shows: the $1M+ order in the BTC fixture and a watched wallet opening its BTC long."""
    monkeypatch.setattr(whales, "request", senpi)
    monkeypatch.setattr(whales, "info", fake_info)
    whales._tracked[0] = 0.0                       # the fixture's taker is on the tracked list
    alerts, trades = [], load("trades_snapshot")["data"]
    whales.on_trades(trades, 0, set(), 1_000_000, alerts.append)
    monkeypatch.setattr(whales.time, "time", lambda: trades[-1]["time"] / 1000 + 60)
    whales.poll([USER], [""], {USER: {}}, 1_000_000, alerts.append)
    return "\n".join(alerts)


def test_readme_example_is_these_fixtures(monkeypatch):
    assert readme_example(monkeypatch) in (ROOT / "README.md").read_text()
