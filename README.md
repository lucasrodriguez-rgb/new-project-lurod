# Polymarket Index

A wallet tracking and copy-trading system for [Polymarket](https://polymarket.com) — a "Polymarket S&P 500" that identifies and mirrors the highest-performing prediction market wallets.

## How It Works

1. **Discover wallets** from the Polymarket leaderboard, on-chain activity scans, and a manual seed list
2. **Score wallets** on ROI, Sharpe ratio, win rate, volume, and market diversity over configurable lookback windows
3. **Maintain a ranked index** of the top N wallets (default 50), updated hourly
4. **Detect new trades** from indexed wallets by polling every 60 seconds
5. **Validate signals** against liquidity, timing, crowding, and staleness filters
6. **Build copy-trade orders** with dynamic position sizing (1–2% of portfolio)
7. **Execute orders** via the Polymarket CLOB API (or log them in dry-run mode)

## Architecture

```
polymarket_index/
├── main.py                      # Async orchestrator / entry point
├── config.py                    # Settings loaded from .env (pydantic-settings)
├── api/
│   ├── polymarket.py            # Gamma + CLOB API client (rate-limited, typed)
│   └── onchain.py               # Polygon RPC reader (wallet discovery)
├── tracker/
│   ├── wallet_scanner.py        # Multi-source wallet discovery and ingestion
│   ├── performance.py           # Scoring engine (ROI, Sharpe, win rate, etc.)
│   └── leaderboard.py           # Ranked index with enter/exit events
├── signals/
│   ├── detector.py              # Poll-based new-trade detection
│   └── validator.py             # 7-check signal filter pipeline
├── executor/
│   ├── trade_builder.py         # Position sizing + order construction
│   └── order_manager.py         # CLOB order placement + lifecycle
├── backtest/
│   ├── data_collector.py        # Fetch + cache historical data from Gamma API
│   ├── engine.py                # Walk-forward backtest engine
│   └── runner.py                # CLI entry point with result reporting
├── live/
│   ├── paper_portfolio.py       # Virtual portfolio with P&L tracking
│   └── paper_trader.py          # Real-time paper trading engine + dashboard
├── db/
│   ├── models.py                # SQLAlchemy async models (5 tables)
│   └── queries.py               # Typed query functions
└── tests/
```

## Stack

- **Python 3.11+** with `asyncio` + `aiohttp`
- **SQLAlchemy 2.0** (async) with SQLite (local dev) or PostgreSQL
- **Polymarket Gamma API** for markets, positions, trade history
- **Polymarket CLOB API** for order books and order placement
- **web3.py** for Polygon on-chain reads
- **loguru** for structured logging
- **pydantic-settings** for typed configuration

## Scoring Formula

Each wallet receives a composite score (0–100):

```
SCORE = (0.35 × ROI) + (0.25 × Sharpe) + (0.20 × Win Rate) + (0.10 × Volume) + (0.10 × Diversity)
```

All component scores are normalized 0–100. Recent trades are weighted more heavily via exponential decay (14-day half-life).

## Signal Validation

Raw signals must pass all 7 checks:

| Check | Rule |
|-------|------|
| Signal age | Must be < 5 minutes old |
| Market status | Not closed, paused, or disputed |
| Liquidity | Market has > $10k liquidity |
| Time to close | Market resolves > 24h from now |
| Price staleness | Within 5% of current order book mid |
| Crowding | < 3 top wallets already hold the same position |
| Wallet tenure | Wallet has been in index > 48 hours |

## Live Paper Trading

Run a real-time simulation that tracks top wallets and copies their trades into a virtual portfolio — no API keys needed:

```bash
# Track top 500 wallets for 24 hours
python3 -m polymarket_index.live --wallets 500 --duration 24

# Aggressive mode: $50k capital, bigger positions, faster polling
python3 -m polymarket_index.live --wallets 500 --capital 50000 --trade-pct 0.02 --poll 15

# Resume a previous session
python3 -m polymarket_index.live --resume

# Quick test: 100 wallets, 1 hour
python3 -m polymarket_index.live --wallets 100 --duration 1
```

The paper trader:
- Fetches the top N wallets from the Polymarket leaderboard
- Scores each wallet on ROI, Sharpe, win rate, volume, and diversity
- Polls for new trades every 30s (configurable)
- Copies trades from top-scored wallets with dynamic position sizing
- Resolves positions when markets close
- Prints a live dashboard with P&L, open positions, win rate, and recent signals
- Saves state to `live_paper_trading.json` every cycle (auto-resumes on restart)

## Backtesting

The backtest module lets you validate the strategy against real historical data **before risking any capital**. No API keys required — it uses the public Gamma API.

### Quick Start

```bash
pip install -r requirements.txt

# Step 1: Fetch and cache historical trade data from top wallets
python3 -m polymarket_index.backtest.runner collect

# Step 2: Run the backtest
python3 -m polymarket_index.backtest.runner run
```

### Or combine both steps:

```bash
python3 -m polymarket_index.backtest.runner full
```

### Customize Parameters

```bash
# Use specific wallets instead of the leaderboard
python3 -m polymarket_index.backtest.runner full \
  --wallets 0xabc123...,0xdef456...

# Adjust strategy parameters
python3 -m polymarket_index.backtest.runner run \
  --capital 50000 \
  --top-n 10 \
  --lookback 60 \
  --trade-pct 0.02

# Set date range
python3 -m polymarket_index.backtest.runner run \
  --start 2025-06-01 \
  --end 2025-09-01

# Export results
python3 -m polymarket_index.backtest.runner run \
  --output results.json \
  --csv equity_curve.csv
```

### What the Backtest Does

1. **Scores wallets** using only data from a rolling lookback window (no lookahead bias)
2. **Walks forward day-by-day** through the trading window
3. **Detects trades** from the top-N scored wallets
4. **Validates** against crowding limits and position constraints
5. **Simulates copy-trades** with dynamic position sizing
6. **Resolves positions** when market outcomes are known
7. **Reports** total return, Sharpe ratio, max drawdown, win rate, profit factor, and daily equity curve

Data is cached locally in `backtest_data/` so you only hit the API once. Subsequent runs are instant.

## Live System

### Getting Started

```bash
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys and settings

# Run in dry-run mode (no real orders)
python3 -m polymarket_index.main

# Run with live order placement
python3 -m polymarket_index.main --live
```

### What You Need

| Credential | Required For | How to Get |
|------------|-------------|------------|
| None | Backtesting | Just run it — uses public API |
| `POLYGON_RPC_URL` | On-chain wallet discovery | Free tier from [Alchemy](https://www.alchemy.com/) or [Infura](https://www.infura.io/) |
| `POLYMARKET_API_KEY` | Live order placement | [Polymarket CLOB docs](https://docs.polymarket.com/) |
| `POLYMARKET_SECRET` | Live order placement | Same as above |
| `POLYMARKET_PASSPHRASE` | Live order placement | Same as above |

## Configuration

All settings are loaded from `.env` via pydantic-settings. Key parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `TOP_N_WALLETS` | 50 | Number of wallets in the index |
| `SCORE_LOOKBACK_DAYS` | 30 | Lookback window for scoring |
| `MIN_MARKET_LIQUIDITY` | 10000 | Minimum market liquidity ($) |
| `MIN_HOURS_TO_CLOSE` | 24 | Minimum hours until market resolution |
| `BASE_TRADE_PCT` | 0.01 | Base position size (% of portfolio) |
| `POLL_INTERVAL_SECONDS` | 60 | Signal detection poll frequency |
| `PORTFOLIO_VALUE_USDC` | 10000 | Portfolio value for position sizing |

## Database

Uses SQLAlchemy 2.0 async with 5 tables:

- **wallets** — tracked addresses with scores and index status
- **trades** — full trade history per wallet
- **signals** — detected signals with validation results
- **orders** — copy-trade orders with lifecycle status
- **leaderboard_snapshots** — hourly index snapshots

Default: SQLite for local development. Set `DATABASE_URL` to a PostgreSQL connection string for production.
