"""
Run the quant strategy.

Usage:
    python3 -m polymarket_index.quant              # dry run
    python3 -m polymarket_index.quant --live        # live trading
"""
import asyncio
import sys

from loguru import logger
from polymarket_index.quant.strategy import main

logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <level>{message}</level>", level="INFO")
logger.add("logs/quant_{time:YYYY-MM-DD}.log", rotation="1 day", level="DEBUG")

asyncio.run(main())
