"""Browser-oriented runner environment."""

from __future__ import annotations

from typing import Dict

from .base import BaseRunner


class BrowserRunner(BaseRunner):
    runner_name = "browser"

    def extra_environment(self) -> Dict[str, str]:
        return {
            "AIRDROP_BROWSER_HEADLESS": "1",
            "BROWSER_HEADLESS": "1",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        }
