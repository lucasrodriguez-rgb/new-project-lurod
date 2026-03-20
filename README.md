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

## Getting Started

```bash
# Clone and install
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys and settings

# Run in dry-run mode (no real orders)
python3 -m polymarket_index.main

# Run with live order placement
python3 -m polymarket_index.main --live
```

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
