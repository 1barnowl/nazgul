#!/usr/bin/env python3
"""
impermanent_loss_hedge_bot.py — Impermanent‑Loss Hedge Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When providing liquidity on Uniswap V2, this bot buys a
protective put option on the risky asset (ETH or WBTC)
via Hegic.  It calculates the required hedge size based on
your LP exposure and executes the option purchase on chain.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `impermanent_loss_hedge_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "dry_run": false,
  "lp_token": "0xLpTokenAddress",
  "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
  "hedge_asset": "token0",           // which asset to hedge (must be WETH or WBTC)
  "option_provider": {
    "name": "hegic",
    "contract": "0xEfC0eEDf1c9d0F1F9b6d5d0a7b6e4e7D70e5c27b",   // HegicETHOptions or HegicWBTOptions
    "premium_token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",   // USDC
    "period_days": 7,
    "strike_percent_below": 10.0,    // % below current spot
    "max_premium_percent": 3.0       // max premium as % of hedged notional
  },
  "poll_interval_hours": 24,
  "state_file": "impermanent_loss_hedge_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from web3 import Web3

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "impermanent_loss_hedge_bot"
BOT_NAME = "Impermanent‑Loss Hedge"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "impermanent_loss_hedge_config.json"
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
UNISWAP_V2_PAIR_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"getReserves","outputs":[{"name":"reserve0","type":"uint112"},{"name":"reserve1","type":"uint112"},{"name":"blockTimestampLast","type":"uint32"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

# Hegic options contract ABI (minimal, matches the create/getPremium functions)
HEGIC_OPTIONS_ABI = json.loads('''[
    {"constant":false,"inputs":[{"name":"period","type":"uint256"},{"name":"amount","type":"uint256"},{"name":"strike","type":"uint256"},{"name":"optionType","type":"uint8"}],"name":"create","outputs":[{"name":"optionID","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"constant":true,"inputs":[{"name":"period","type":"uint256"},{"name":"amount","type":"uint256"},{"name":"strike","type":"uint256"},{"name":"optionType","type":"uint8"}],"name":"getPremium","outputs":[{"name":"premium","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"constant":true,"inputs":[{"name":"","type":"uint256"}],"name":"options","outputs":[{"name":"state","type":"uint8"},{"name":"holder","type":"address"},{"name":"strike","type":"uint256"},{"name":"amount","type":"uint256"},{"name":"lockedAmount","type":"uint256"},{"name":"premium","type":"uint256"},{"name":"expiration","type":"uint256"},{"name":"optionType","type":"uint8"}],"stateMutability":"view","type":"function"}
]''')

# Chainlink price feed ABI (get latest price)
CHAINLINK_PRICE_FEED_ABI = json.loads('''[
    {"inputs":[],"name":"latestRoundData","outputs":[{"internalType":"uint80","name":"roundId","type":"uint80"},{"internalType":"int256","name":"answer","type":"int256"},{"internalType":"uint256","name":"startedAt","type":"uint256"},{"internalType":"uint256","name":"updatedAt","type":"uint256"},{"internalType":"uint80","name":"answeredInRound","type":"uint80"}],"stateMutability":"view","type":"function"}
]''')

# ── Chainlink price feed addresses (Ethereum mainnet) ────────────
PRICE_FEEDS = {
    "WETH": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",   # ETH/USD
    "WBTC": "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"    # BTC/USD
}

# ── Option purchase logic ────────────────────────────────────────
def _approve_if_needed(w3, token_addr, spender, account, amount, gas_price, gas_limit, private_key):
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    allowance = token.functions.allowance(account.address, spender).call()
    if allowance >= amount:
        return True
    try:
        tx = token.functions.approve(spender, amount).build_transaction({
            "from": account.address,
            "gas": 100000,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return True
    except Exception as e:
        _post(f"Approve error: {e}", "error")
        return False

def _get_asset_price(w3, asset_symbol: str) -> float:
    """Retrieve USD price from Chainlink."""
    feed_addr = PRICE_FEEDS.get(asset_symbol)
    if not feed_addr:
        _post(f"No price feed for {asset_symbol}", "error")
        return 0.0
    feed = w3.eth.contract(address=Web3.to_checksum_address(feed_addr), abi=CHAINLINK_PRICE_FEED_ABI)
    try:
        round_data = feed.functions.latestRoundData().call()
        price = round_data[1] / 1e8  # Chainlink usually returns 8 decimals
        return float(price)
    except Exception as e:
        _post(f"Chainlink error for {asset_symbol}: {e}", "error")
        return 0.0

def _get_hedge_amount(w3, lp_token_addr, hedge_asset_idx, account, router_addr) -> tuple:
    """
    Returns (asset_address, asset_decimals, user_exposure_in_token_units, price_usd).
    """
    pair = w3.eth.contract(address=lp_token_addr, abi=UNISWAP_V2_PAIR_ABI)
    token0_addr = pair.functions.token0().call()
    token1_addr = pair.functions.token1().call()
    token0_dec = w3.eth.contract(address=token0_addr, abi=ERC20_ABI).functions.decimals().call()
    token1_dec = w3.eth.contract(address=token1_addr, abi=ERC20_ABI).functions.decimals().call()

    reserves = pair.functions.getReserves().call()
    total_supply = pair.functions.totalSupply().call()
    lp_balance = w3.eth.contract(address=lp_token_addr, abi=ERC20_ABI).functions.balanceOf(account.address).call()

    if lp_balance == 0 or total_supply == 0:
        return None, 0, 0, 0.0

    user_share = lp_balance / total_supply
    # Amount of each token owned by user
    if hedge_asset_idx == 0:
        reserve = reserves[0]
        asset_addr = token0_addr
        decimals = token0_dec
        asset_symbol = "WETH" if asset_addr.lower() == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2".lower() else \
                       "WBTC" if asset_addr.lower() == "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599".lower() else None
    else:
        reserve = reserves[1]
        asset_addr = token1_addr
        decimals = token1_dec
        asset_symbol = "WETH" if asset_addr.lower() == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2".lower() else \
                       "WBTC" if asset_addr.lower() == "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599".lower() else None

    if not asset_symbol:
        return None, 0, 0, 0.0

    user_exposure = reserve * user_share
    # Convert to human units
    user_exposure_units = user_exposure / (10 ** decimals)

    price_usd = _get_asset_price(w3, asset_symbol)
    return asset_addr, decimals, user_exposure_units, price_usd, asset_symbol

def _buy_hedge(w3, config, account, private_key,
               option_contract_addr, premium_token_addr,
               asset_addr, asset_decimals, hedge_amount_units,
               current_price, strike_pct_below, period_days,
               max_premium_pct, dry_run):
    option_contract = w3.eth.contract(address=option_contract_addr, abi=HEGIC_OPTIONS_ABI)
    gas_price = int(w3.eth.gas_price * 1.1)
    gas_limit = 500000

    # Hegic expects amount in wei of the underlying asset (e.g., 1e18 for 1 ETH)
    hedge_amount = int(hedge_amount_units * (10 ** asset_decimals))
    # Strike price: e.g., spot * (1 - strike_pct_below/100)
    strike_usd = current_price * (1 - strike_pct_below / 100.0)
    # Hegic uses strike in 8 decimals (like Chainlink)
    strike_wei = int(strike_usd * 1e8)

    # Option type: 0 = Call, 1 = Put
    option_type = 1
    period = period_days * 86400  # Hegic expects seconds

    # Get premium
    try:
        premium = option_contract.functions.getPremium(period, hedge_amount, strike_wei, option_type).call()
    except Exception as e:
        _post(f"getPremium error: {e}", "error")
        return False

    premium_token = w3.eth.contract(address=premium_token_addr, abi=ERC20_ABI)
    premium_dec = premium_token.functions.decimals().call()
    premium_human = premium / (10 ** premium_dec)
    # Check max premium
    notional = hedge_amount_units * current_price
    max_premium_allowed = notional * max_premium_pct / 100.0
    if premium_human > max_premium_allowed:
        _post(f"Premium {premium_human:.2f} exceeds max {max_premium_allowed:.2f} USD", "warning")
        return False

    if dry_run:
        _post(f"Dry-run: would buy put on {hedge_amount_units:.4f} tokens, strike {strike_usd:.2f}, premium {premium_human:.2f}", "info")
        return True

    # Approve premium token
    if not _approve_if_needed(w3, premium_token_addr, option_contract_addr, account, premium, gas_price, gas_limit, private_key):
        return False

    # Buy option
    try:
        tx = option_contract.functions.create(period, hedge_amount, strike_wei, option_type).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            _post(f"Bought put option: ID {receipt.logs[0].topics[0].hex() if receipt.logs else '?'} "
                  f"size {hedge_amount_units:.4f}, strike {strike_usd:.2f}", "info")
            return True
        else:
            _post("Option purchase failed", "error")
            return False
    except Exception as e:
        _post(f"Option creation error: {e}", "error")
        return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Impermanent‑Loss Hedge Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        rpc = config["rpc_url"]
        w3 = Web3(Web3.HTTPProvider(rpc))
        if not w3.is_connected():
            _post("RPC not available", "error")
            time.sleep(60)
            continue

        private_key = config.get("private_key")
        dry_run = config.get("dry_run", False)
        if not private_key and not dry_run:
            _post("Private key missing", "error")
            time.sleep(3600)
            continue

        account = None
        if private_key:
            try:
                account = w3.eth.account.from_key(private_key)
            except Exception:
                _post("Invalid private key", "error")
                time.sleep(3600)
                continue

        lp_token = Web3.to_checksum_address(config["lp_token"])
        router = Web3.to_checksum_address(config["router"])
        hedge_asset = config.get("hedge_asset", "token0")
        hedge_idx = 0 if hedge_asset.lower() == "token0" else 1

        opt_cfg = config.get("option_provider", {})
        opt_contract = Web3.to_checksum_address(opt_cfg["contract"])
        premium_token = Web3.to_checksum_address(opt_cfg["premium_token"])
        period_days = int(opt_cfg.get("period_days", 7))
        strike_pct = float(opt_cfg.get("strike_percent_below", 10.0))
        max_premium_pct = float(opt_cfg.get("max_premium_percent", 3.0))

        # Fetch exposure
        asset_addr, asset_dec, exposure_units, price, asset_symbol = _get_hedge_amount(w3, lp_token, hedge_idx, account, router)
        if asset_addr is None:
            _post("Cannot determine hedge asset (must be WETH or WBTC). Skipping.", "warning")
        else:
            if exposure_units <= 0:
                _post("No LP exposure to hedge", "info")
            else:
                # Load state and check if we already bought an option recently
                state_file = config.get("state_file", "impermanent_loss_hedge_state.json")
                try:
                    with open(state_file, "r") as f:
                        state = json.load(f)
                except Exception:
                    state = {}
                last_hedge = state.get("last_hedge_time", 0)
                now = time.time()
                poll_hours = config.get("poll_interval_hours", 24)
                if now - last_hedge > poll_hours * 3600:
                    _post(f"Exposure: {exposure_units:.4f} {asset_symbol} @ ${price:.2f}. Buying hedge...", "info")
                    success = _buy_hedge(w3, config, account, private_key,
                                         opt_contract, premium_token,
                                         asset_addr, asset_dec, exposure_units,
                                         price, strike_pct, period_days,
                                         max_premium_pct, dry_run)
                    if success:
                        state["last_hedge_time"] = now
                        with open(state_file, "w") as f:
                            json.dump(state, f, indent=2)
                else:
                    _post(f"Skipping hedge: last purchase was {timedelta(seconds=now - last_hedge)} ago", "info")

        poll_seconds = int(config.get("poll_interval_hours", 24)) * 3600
        _heartbeat()
        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
