#!/usr/bin/env python3
"""
governance_token_dumper_bot.py — Governance Token Dumper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Harvests governance tokens (UNI, AAVE, CRV, etc.) from staking
contracts as soon as they are claimable and sells them for
stablecoins on Uniswap V2, locking in profits.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `governance_dumper_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "stablecoin": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
  "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
  "reward_contracts": [
    {
      "address": "0xStakingContract1",
      "reward_token": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
      "min_reward_usd": 5.0
    }
  ],
  "max_slippage_pct": 2.0,
  "gas_limit": 400000,
  "gas_price_multiplier": 1.1,
  "poll_interval_seconds": 3600,
  "heartbeat_interval": 30
}

Each entry in reward_contracts must expose:
  - earned(address account) → uint256 (reward token balance)
  - getReward()
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "governance_token_dumper_bot"
BOT_NAME = "Governance Token Dumper"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "governance_dumper_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID,
            "bot_name": BOT_NAME,
            "summary": summary,
            "level": level,
            "payload": payload or {},
        }, timeout=5)
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status": "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── ABIs ─────────────────────────────────────────────────────────
STAKING_REWARD_ABI = json.loads('''[
    {"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"earned","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[],"name":"getReward","outputs":[],"type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]''')

# ── Price oracle (Chainlink / direct) ────────────────────────────
# For simplicity, use Uniswap's getAmountsOut to estimate value in USD (using WETH path)
# or a CoinGecko API. We'll use CoinGecko for rough estimate: get token price.
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
def get_token_price_usd(token_address: str, platform: str = "ethereum") -> Optional[float]:
    """Fetch token price in USD using CoinGecko simple API."""
    try:
        resp = requests.get(COINGECKO_URL, params={
            "ids": token_address,       # CoinGecko expects contract address, works
            "vs_currencies": "usd"
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get(token_address.lower(), {}).get("usd")
    except Exception:
        pass
    return None

# ── Claim and dump ───────────────────────────────────────────────
def claim_and_dump(config: dict) -> None:
    rpc = config["rpc_url"]
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        _post("RPC connection failed", "error")
        return

    account = w3.eth.account.from_key(config["private_key"])
    sender = account.address
    stablecoin = Web3.to_checksum_address(config["stablecoin"])
    router_addr = Web3.to_checksum_address(config["router"])
    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)

    gas_mult = float(config.get("gas_price_multiplier", 1.1))
    gas_limit = int(config.get("gas_limit", 400000))

    reward_contracts = config.get("reward_contracts", [])

    for entry in reward_contracts:
        contract_addr = Web3.to_checksum_address(entry["address"])
        reward_token_addr = Web3.to_checksum_address(entry["reward_token"])
        min_reward_usd = float(entry.get("min_reward_usd", 5.0))

        staking = w3.eth.contract(address=contract_addr, abi=STAKING_REWARD_ABI)
        reward_token = w3.eth.contract(address=reward_token_addr, abi=ERC20_ABI)

        # Check claimable rewards
        try:
            earned = staking.functions.earned(sender).call()
        except Exception as e:
            _post(f"earned() failed for {contract_addr}: {e}", "error")
            continue

        if earned == 0:
            continue

        # Estimate USD value
        price = get_token_price_usd(reward_token_addr.lower())
        if price is None:
            _post(f"Cannot fetch price for reward token {reward_token_addr}, skipping", "warning")
            continue

        decimals = reward_token.functions.decimals().call()
        amount_units = earned / 10**decimals
        value_usd = amount_units * price
        if value_usd < min_reward_usd:
            _post(f"Reward {amount_units:.2f} tokens (${value_usd:.2f}) below min ${min_reward_usd}, skipping", "info")
            continue

        # Claim
        gas_price = int(w3.eth.gas_price * gas_mult)
        try:
            tx = staking.functions.getReward().build_transaction({
                "from": sender,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(sender)
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status != 1:
                _post(f"Claim failed for {contract_addr} (tx {tx_hash.hex()})", "error")
                continue
        except Exception as e:
            _post(f"Claim transaction error: {e}", "error")
            continue

        # Now swap reward tokens to stablecoin
        # Approve router
        reward_balance = reward_token.functions.balanceOf(sender).call()
        if reward_balance == 0:
            continue

        # Check allowance
        allowance = reward_token.functions.allowance(sender, router_addr).call()
        if allowance < reward_balance:
            try:
                approve_tx = reward_token.functions.approve(router_addr, reward_balance).build_transaction({
                    "from": sender,
                    "gas": 100000,
                    "gasPrice": gas_price,
                    "nonce": w3.eth.get_transaction_count(sender)
                })
                signed = account.sign_transaction(approve_tx)
                approve_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
            except Exception as e:
                _post(f"Approve failed: {e}", "error")
                continue

        # Get min out
        path = [reward_token_addr, stablecoin]
        if reward_token_addr != stablecoin:
            # Need WETH path if no direct pair? Many tokens have direct USDC pairs on Uniswap V2.
            # Use getAmountsOut to check
            try:
                amounts_out = router.functions.getAmountsOut(reward_balance, path).call()
            except Exception:
                # Try via WETH
                weth = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
                path = [reward_token_addr, weth, stablecoin]
                try:
                    amounts_out = router.functions.getAmountsOut(reward_balance, path).call()
                except Exception as e:
                    _post(f"No viable swap path: {e}", "error")
                    continue
            min_out = int(amounts_out[-1] * (100 - config["max_slippage_pct"]) / 100)
        else:
            min_out = reward_balance  # same token, not needed

        deadline = int(time.time()) + 300
        try:
            tx = router.functions.swapExactTokensForTokens(
                reward_balance, min_out, path, sender, deadline
            ).build_transaction({
                "from": sender,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(sender)
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                _post(f"Dumped {amount_units:.2f} tokens for ~${value_usd:.2f} in stablecoin", "info", {
                    "contract": contract_addr,
                    "reward_token": reward_token_addr,
                    "claimed": amount_units,
                    "estimated_usd": value_usd
                })
            else:
                _post(f"Swap failed (tx {tx_hash.hex()})", "error")
        except Exception as e:
            _post(f"Swap error: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Governance Token Dumper Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        poll_interval = int(config.get("poll_interval_seconds", 3600))

        try:
            claim_and_dump(config)
        except Exception as e:
            _post(f"Unexpected error in cycle: {e}", "error")

        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
