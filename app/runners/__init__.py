"""Execution adapters for the two supported AirDrop script families."""

from .base import BaseRunner
from .browser import BrowserRunner
from .web3 import Web3Runner
from .remote import RemoteRunner
from app.config import settings


def create_runner(spec: dict) -> BaseRunner:
    if spec.get("platform") == "windows_host" and settings.windows_worker_url:
        return RemoteRunner(spec)
    runner_type = spec.get("runner", "legacy")
    if runner_type == "browser":
        return BrowserRunner(spec)
    if runner_type == "web3":
        return Web3Runner(spec)
    return BaseRunner(spec)


__all__ = ["BaseRunner", "BrowserRunner", "Web3Runner", "RemoteRunner", "create_runner"]
