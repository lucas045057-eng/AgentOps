"""Web3/wallet-oriented runner environment."""

from __future__ import annotations

from typing import Dict

from .base import BaseRunner


class Web3Runner(BaseRunner):
    runner_name = "web3"

    def extra_environment(self) -> Dict[str, str]:
        return {
            "AIRDROP_CHAIN_MODE": "confirmed-or-unknown",
            "WEB3_CONFIRMATION_REQUIRED": "1",
        }
