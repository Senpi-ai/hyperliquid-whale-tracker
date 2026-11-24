# hyperliquid-whale-tracker

hyperliquid-whale-tracker is a free, open-source Python script from [Senpi](https://senpi.ai) that tracks whale movements on Hyperliquid: it prints every big trade as it happens, with what top traders hold in that market, and alerts you when a wallet you watch opens, closes, flips or resizes a position.

For a free live view of where the top traders and the biggest whales are positioned, see [senpi.ai/traders](https://senpi.ai/traders).

## Quick start

```bash
git clone https://github.com/Senpi-ai/hyperliquid-whale-tracker.git
cd hyperliquid-whale-tracker
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 whales.py
```

That follows the 20 markets with the most 24h volume, on Hyperliquid's main perp dex and its HIP-3 dexes, and prints every order of $1M or more until you press Ctrl+C. No account, no API key. Python 3.9 or newer; the one dependency is [websockets](https://pypi.org/project/websockets/), pinned.

```bash
python3 whales.py --min-usd 250000 --assets BTC,ETH,kPEPE,xyz:TSLA
python3 whales.py --top 40 --watch 0xFIRST_WALLET,0xSECOND_WALLET
python3 whales.py --webhook https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

| Option | Default | |
|---|---|---|
| `--min-usd` | `1000000` | Smallest order or position change to show, in dollars. |
| `--assets` | | Markets to follow, as Hyperliquid names them: `BTC`, `kPEPE`, `xyz:TSLA`. |
| `--top` | `20` | Follow the N markets with the most 24h volume instead. |
| `--watch` | | Wallets to check every 60 seconds, comma separated. Alerts when a position opens, closes, flips or changes by at least `--min-usd`. |
| `--webhook` | | Post each alert as JSON with a `text` field. Slack: an incoming webhook URL. Discord: the channel's webhook URL with `/slack` added at the end. |

The tests run offline: `pip install pytest -r requirements.txt && pytest -q`

## What it shows

For each order at or above `--min-usd`: the time (UTC), market, side (BUY or SELL, from the taker's side), size, average price, notional, the buyer and the seller, a link to the taker's page on Senpi, and one line from Senpi's free asset file: what Senpi's top traders hold long and short in that market, with a link to its Senpi page. Wallets show as 0x plus their last 5 characters; links carry the full address.

For each watched wallet: one line when a position opens, closes, flips or changes by at least `--min-usd`, with a link to the wallet's page on Senpi.

## Example output

From the offline test fixtures: real responses with every wallet address, transaction hash and trade id replaced by a made-up one, and times moved. A test checks that these lines match what the script prints for the fixtures.

```
$ python3 whales.py --assets BTC --watch 0x00000000000000000000000000000000000000aa
2026-11-18 20:19:33 UTC  BTC  SELL 17.86 @ 61,405 = $1.1M  buyer 0x00001 +10 more, seller 0x00002
  taker: https://senpi.ai/traders/0x0000000000000000000000000000000000000002
  Senpi top traders: $139.2M long (29 wallets), $309.6M short (46 wallets). https://senpi.ai/assets/BTC
2026-11-18 20:20:39 UTC  0x000aa opened a $1.5M BTC long
  https://senpi.ai/traders/0x00000000000000000000000000000000000000aa
```

The first alert is one taker order: a sell that filled against 11 resting orders and reached the feed as 13 prints, the largest $450K. The second is the watched wallet's BTC long showing up on the next 60-second check.

## How it works

- **Markets.** `POST https://api.hyperliquid.xyz/info` with `perpDexs`, then `metaAndAssetCtxs` once per perp dex, for every listed market and its 24h volume. Names are checked before subscribing, because Hyperliquid closes the whole connection on a subscription to a market that does not exist.
- **Trades.** One `trades` subscription per market on `wss://api.hyperliquid.xyz/ws`. Each print carries the buyer and the seller, and its side is the taker's.
- **One order, one alert.** A big market order fills against many resting orders and each fill arrives as its own print, so the script adds the prints of each taker order back together (same block, transaction and side). A $5M sweep shows as one $5M order, not as two hundred prints that each look small.
- **Staying connected.** A ping every 30 seconds (Hyperliquid closes connections that go 60 seconds without a message) and a reconnect 5 seconds after any drop. Each subscription starts with a replay of recent trades: anything from before the script started is skipped, and nothing alerts twice.
- **Watching wallets.** `clearinghouseState` for each wallet on every perp dex, every 60 seconds, compared with the last read.
- **Context.** `https://senpi.ai/assets/<symbol>/data.json`, read at most every 5 minutes per market, the file's own cache time.

## Limits

- Perp markets only; spot trades are not followed.
- The price shown is the order's average: notional divided by size.
- Hyperliquid limits each IP to 10 websocket connections, 1,000 subscriptions and 1,200 request weight a minute. Each watched wallet costs one `clearinghouseState` call (weight 2) per perp dex per minute.
- The Senpi line appears only for markets that have a Senpi page.
- Alerts go out as they happen, so a slow webhook slows the feed.

## Go further with Senpi

- [senpi.ai/traders](https://senpi.ai/traders) is a free live view of where the top traders and the biggest whales are positioned.
- [Senpi](https://senpi.ai) is an AI trading agent for Hyperliquid. Ask your Senpi agent, or Claude/ChatGPT with the [Senpi plugin](https://github.com/Senpi-ai/senpi-plugin), about any wallet or market; Senpi tools need a Senpi sign-in, this script does not.
- Open-source Senpi skills and strategies: [senpi-skills](https://github.com/Senpi-ai/senpi-skills).

Not investment advice: a big trade shows what someone did, not what you should do.
