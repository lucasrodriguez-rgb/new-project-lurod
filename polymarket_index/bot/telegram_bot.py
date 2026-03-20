"""
Telegram bot for trade alerts and portfolio monitoring.

Commands:
  /status  — current portfolio status
  /pnl     — P&L summary
  /positions — open positions
  /trades  — recent trade history
  /help    — list commands
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable, Awaitable

from loguru import logger

from polymarket_index.config import settings
from polymarket_index.edge.trade_db import TradeDB

try:
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
    )
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False


class TelegramBot:
    """
    Telegram bot that provides trade alerts and monitoring commands.
    Can run standalone or alongside the EV trader.
    """

    def __init__(self, trade_db: TradeDB | None = None) -> None:
        if not HAS_TELEGRAM:
            raise ImportError("python-telegram-bot is not installed")

        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._db = trade_db or TradeDB()
        self._app: Application | None = None
        self._portfolio_callback: Callable | None = None

    def set_portfolio_callback(self, callback: Callable) -> None:
        self._portfolio_callback = callback

    async def send_alert(self, message: str) -> None:
        """Send a message to the configured chat."""
        if not self._token or not self._chat_id:
            return
        try:
            if self._app and self._app.bot:
                await self._app.bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode="Markdown",
                )
            else:
                from telegram import Bot
                bot = Bot(token=self._token)
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode="Markdown",
                )
        except Exception as exc:
            logger.warning("Telegram alert failed: {}", exc)

    async def start_polling(self) -> None:
        """Start the bot in polling mode (blocking)."""
        if not self._token:
            logger.warning("No TELEGRAM_BOT_TOKEN configured, bot disabled")
            return

        self._app = Application.builder().token(self._token).build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))

        logger.info("Telegram bot starting...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # ── Command handlers ────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 *Polymarket EV Trader Bot*\n\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "📋 *Commands*\n\n"
            "/status — Portfolio overview\n"
            "/pnl — Profit & loss summary\n"
            "/positions — Open positions\n"
            "/trades — Recent trade history\n"
            "/help — This message",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        stats = self._db.get_stats()
        now = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S UTC")

        msg = (
            f"📊 *Status* ({now})\n\n"
            f"Total trades: {stats['total_trades']}\n"
            f"Open: {stats['open']}\n"
            f"Closed: {stats['closed']}\n"
            f"Win rate: {stats['win_rate']:.1f}%\n"
            f"P&L: ${stats['total_pnl']:+,.2f}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        stats = self._db.get_stats()
        history = self._db.get_pnl_history(10)

        msg = (
            f"💰 *P&L Summary*\n\n"
            f"Total P&L: ${stats['total_pnl']:+,.2f}\n"
            f"Win rate: {stats['win_rate']:.1f}% ({stats['wins']}W / {stats['losses']}L)\n"
            f"Avg win: ${stats['avg_win']:+,.2f}\n"
            f"Avg loss: ${stats['avg_loss']:+,.2f}\n"
        )

        if history:
            msg += "\n📈 *Recent snapshots:*\n"
            for h in history[-5:]:
                ts = h["timestamp"][11:19]
                msg += f"`{ts}` Value: ${h['total_value']:,.0f} PnL: ${h['realized_pnl']:+,.0f}\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        open_trades = self._db.get_open_trades()

        if not open_trades:
            await update.message.reply_text("📭 No open positions.")
            return

        msg = f"📈 *Open Positions* ({len(open_trades)})\n\n"
        for t in open_trades[:15]:
            msg += (
                f"• {t.side} ${t.size_usdc:.0f} @ {t.entry_price:.3f} "
                f"(EV {t.ev_pct:.0%})\n"
                f"  _{t.market_question[:45]}_\n"
            )

        if len(open_trades) > 15:
            msg += f"\n_...and {len(open_trades) - 15} more_"

        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        recent = self._db.get_recent_trades(10)

        if not recent:
            await update.message.reply_text("📭 No trades yet.")
            return

        msg = "📋 *Recent Trades*\n\n"
        for t in recent:
            icon = "✅" if t.pnl and t.pnl > 0 else ("❌" if t.pnl and t.pnl < 0 else "⏳")
            pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "pending"
            msg += (
                f"{icon} {t.side} ${t.size_usdc:.0f} @ {t.entry_price:.3f} "
                f"→ {pnl_str} [{t.mode}]\n"
                f"  _{t.market_question[:40]}_\n"
            )

        await update.message.reply_text(msg, parse_mode="Markdown")


def create_alert_callback() -> Callable[[str], Awaitable[None]] | None:
    """Create a simple alert sender without running the full bot."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return None

    if not HAS_TELEGRAM:
        return None

    bot = TelegramBot()

    async def _send(msg: str) -> None:
        await bot.send_alert(msg)

    return _send
