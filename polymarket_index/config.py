from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Polymarket API credentials ──────────────────────────────────────
    polymarket_api_key: str = ""
    polymarket_secret: str = ""
    polymarket_passphrase: str = ""

    # ── Endpoints ───────────────────────────────────────────────────────
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    polymarket_leaderboard_url: str = "https://polymarket.com/leaderboard"

    # ── Polygon RPC ─────────────────────────────────────────────────────
    polygon_rpc_url: str = "https://polygon-rpc.com"

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///polymarket_index.db"

    # ── Leaderboard / Scoring ───────────────────────────────────────────
    top_n_wallets: int = 50
    score_lookback_days: int = 30
    score_weight_roi: float = 0.35
    score_weight_sharpe: float = 0.25
    score_weight_win_rate: float = 0.20
    score_weight_volume: float = 0.10
    score_weight_diversity: float = 0.10
    min_score_for_index: float = 20.0
    inactive_days_threshold: int = 14

    # ── Signal validation ───────────────────────────────────────────────
    min_market_liquidity: float = 10_000.0
    min_hours_to_close: int = 24
    max_price_staleness_pct: float = 0.05
    max_crowding_wallets: int = 3
    min_hours_in_index: int = 48
    max_signal_age_seconds: int = 300

    # ── Trade sizing ────────────────────────────────────────────────────
    base_trade_pct: float = 0.01
    boosted_trade_pct: float = 0.02
    reduced_trade_pct: float = 0.005
    top_wallet_boost_rank: int = 10
    slippage_tolerance: float = 0.005
    portfolio_value_usdc: float = 10_000.0

    # ── Polling / scheduling ────────────────────────────────────────────
    poll_interval_seconds: int = 60
    rescore_interval_seconds: int = 3600
    leaderboard_snapshot_interval_seconds: int = 3600

    # ── Rate limiting ───────────────────────────────────────────────────
    api_rate_limit_rps: int = 10
    api_max_retries: int = 5
    api_base_backoff_seconds: float = 1.0

    # ── Seed wallets ────────────────────────────────────────────────────
    seed_wallets: list[str] = Field(default_factory=list)


settings = Settings()
