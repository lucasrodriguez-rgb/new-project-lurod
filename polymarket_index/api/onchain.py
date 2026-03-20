from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from web3 import AsyncWeb3, AsyncHTTPProvider
from web3.types import LogReceipt

from polymarket_index.config import settings

# Polymarket CTF Exchange contract on Polygon
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# ERC-1155 TransferSingle event signature
TRANSFER_SINGLE_TOPIC = AsyncWeb3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()


@dataclass(frozen=True)
class OnChainTrade:
    tx_hash: str
    from_address: str
    to_address: str
    token_id: str
    amount: int
    block_number: int
    timestamp: int


class PolygonReader:
    """Read-only Polygon RPC client for scanning on-chain Polymarket activity."""

    def __init__(self) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(settings.polygon_rpc_url))

    async def is_connected(self) -> bool:
        try:
            return await self._w3.is_connected()
        except Exception:
            return False

    async def get_latest_block(self) -> int:
        return await self._w3.eth.block_number

    async def get_wallet_balance(self, address: str) -> float:
        """Get native MATIC balance in ether units."""
        balance_wei = await self._w3.eth.get_balance(
            AsyncWeb3.to_checksum_address(address)
        )
        return float(AsyncWeb3.from_wei(balance_wei, "ether"))

    async def scan_recent_trades(
        self,
        from_block: int | None = None,
        blocks_back: int = 5000,
        min_amount: int = 0,
    ) -> list[OnChainTrade]:
        """
        Scan recent ERC-1155 TransferSingle events from the CTF Exchange
        to discover active trading wallets.
        """
        latest = await self.get_latest_block()
        start_block = from_block or (latest - blocks_back)

        trades: list[OnChainTrade] = []

        try:
            logs: list[LogReceipt] = await self._w3.eth.get_logs(
                {
                    "fromBlock": start_block,
                    "toBlock": latest,
                    "address": AsyncWeb3.to_checksum_address(CTF_EXCHANGE_ADDRESS),
                    "topics": [TRANSFER_SINGLE_TOPIC],
                }
            )
        except Exception as exc:
            logger.warning("On-chain log scan failed: {}", exc)
            return trades

        for log in logs:
            try:
                topics = log.get("topics", [])
                data = log.get("data", b"")

                if len(topics) < 4:
                    continue

                from_addr = "0x" + topics[2].hex()[-40:]
                to_addr = "0x" + topics[3].hex()[-40:]

                if isinstance(data, (bytes, bytearray)):
                    data_hex = data.hex()
                else:
                    data_hex = str(data).replace("0x", "")

                if len(data_hex) >= 128:
                    token_id = str(int(data_hex[:64], 16))
                    amount = int(data_hex[64:128], 16)
                else:
                    continue

                if amount < min_amount:
                    continue

                trades.append(
                    OnChainTrade(
                        tx_hash=log["transactionHash"].hex(),
                        from_address=from_addr.lower(),
                        to_address=to_addr.lower(),
                        token_id=token_id,
                        amount=amount,
                        block_number=log["blockNumber"],
                        timestamp=0,
                    )
                )

            except Exception as exc:
                logger.debug("Failed to parse log entry: {}", exc)
                continue

        logger.info(
            "Scanned blocks {}-{}: found {} trades",
            start_block,
            latest,
            len(trades),
        )
        return trades

    async def extract_active_wallets(
        self,
        blocks_back: int = 5000,
        min_trade_count: int = 3,
    ) -> list[str]:
        """
        Find wallets with at least `min_trade_count` recent on-chain trades.
        """
        trades = await self.scan_recent_trades(blocks_back=blocks_back)

        wallet_counts: dict[str, int] = {}
        for t in trades:
            for addr in (t.from_address, t.to_address):
                if addr and addr != "0x" + "0" * 40:
                    wallet_counts[addr] = wallet_counts.get(addr, 0) + 1

        return [
            addr
            for addr, count in sorted(
                wallet_counts.items(), key=lambda x: x[1], reverse=True
            )
            if count >= min_trade_count
        ]
