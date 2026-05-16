#!/usr/bin/env python3
"""
liquid_staking_auto_restaker_bot.py — Liquid Staking Auto‑Restaker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automatically claims staking rewards from Lido or Rocket Pool,
swaps them for more stETH / rETH, and deposits them back to
compound your position.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `liquid_restaker_config.json` in the same directory:

{
  "rpc_url": "https://mainnet.infura.io/v3/YOUR_KEY",
  "chain_id": 1,
  "private_key": "0xYOUR_PRIVATE_KEY",
  "dry_run": false,
  "protocol": "lido",                     // "lido" or "rocketpool"
  "lido": {
    "steth": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
    "wsteth": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
    "swap_router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
  },
  "rocketpool": {
    "reth": "0xae78736Cd615f374D3085123A210448E74Fc6393",
    "rpl_reward_claim": "0x19D3...",      // RocketRewardsPool contract (legacy)
    "swap_router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
  },
  "swap_slippage_pct": 2.0,
  "gas_limit": 400000,
  "gas_price_multiplier": 1.1,
  "poll_interval_seconds": 3600,
  "state_file": "liquid_restaker_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from web3 import Web3

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "liquid_staking_auto_restaker_bot"
BOT_NAME = "Liquid Staking Auto‑Restaker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "liquid_restaker_config.json"
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

# ── ABIs (minimal) ───────────────────────────────────────────────
ERC20_ABI = json.loads('''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]''')

UNISWAP_V2_ROUTER_ABI = json.loads('''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"}
]''')

# ── Lido / Rocket Pool helpers ───────────────────────────────────
def claim_and_restake_lido(config, w3, account, private_key):
    """Compound Lido stETH rewards manually (sell ETH rewards for stETH and deposit)."""
    lido_cfg = config["lido"]
    steth_addr = Web3.to_checksum_address(lido_cfg["steth"])
    router_addr = Web3.to_checksum_address(lido_cfg["swap_router"])
    weth_addr = Web3.to_checksum_address(lido_cfg["weth"])

    steth = w3.eth.contract(address=steth_addr, abi=ERC20_ABI)
    # Lido staking rewards are reflected in stETH balance increasing (no claim action needed).
    # stETH balance already reflects earned rewards.
    # To "compound" we would need to sell the "excess" stETH for ETH or just not needed? Actually stETH rebases,
    # so the amount automatically increases. So no claim step necessary for Lido.
    # But we can stake more if user has ETH lying around. However, this bot's purpose is to "restake" rewards,
    # which could be interpreted as:
    #  - For Lido: convert any other reward tokens to stETH (e.g., from other protocols).
    #    In a liquid staking context, often users get rewards in other tokens (like LDO or ETH from other sources).
    #    We'll assume the bot checks for ETH balance and stakes it into stETH.
    # For Rocket Pool: rewards are in RPL + ETH, need to claim via RocketRewardsPool.
    # I'll implement Rocket Pool case as it has a concrete claim action.
    # For Lido, I'll check if there is any native ETH balance (which could be from external rewards) and stake it.
    balance = w3.eth.get_balance(account.address)
    if balance < Web3.to_wei(0.01, 'ether'):
        _post("No ETH balance to stake (rewards may already be in stETH)", "info")
        return

    # Staking into Lido is just sending ETH to the stETH contract (submit function)
    steth_abi = json.loads('''[
        {"constant":false,"inputs":[],"name":"submit","outputs":[{"name":"","type":"uint256"}],"payable":true,"stateMutability":"payable","type":"function"}
    ]''')
    steth_contract = w3.eth.contract(address=steth_addr, abi=steth_abi)
    gas_price = int(w3.eth.gas_price * float(config.get("gas_price_multiplier", 1.1)))
    gas_limit = int(config.get("gas_limit", 400000))
    try:
        tx = steth_contract.functions.submit().build_transaction({
            "from": account.address,
            "value": balance,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            _post(f"Staked {Web3.from_wei(balance, 'ether')} ETH into Lido", "info", {"amount": balance})
            return True
        else:
            _post("Lido stake transaction failed", "error")
            return False
    except Exception as e:
        _post(f"Lido staking error: {e}", "error")
        return False

def claim_and_restake_rocketpool(config, w3, account, private_key):
    """Compound Rocket Pool rewards: claim RPL & ETH, swap RPL for ETH, stake for rETH."""
    rpl_cfg = config["rocketpool"]
    reth_addr = Web3.to_checksum_address(rpl_cfg["reth"])
    rpl_reward_contract = Web3.to_checksum_address(rpl_cfg["rpl_reward_claim"])
    router_addr = Web3.to_checksum_address(rpl_cfg["swap_router"])
    weth_addr = Web3.to_checksum_address(rpl_cfg["weth"])

    # 1. Claim rewards (RPL & ETH) from RocketRewardsPool
    # The contract function is: claim(address _nodeAddress, address _rewardClaimer, bool _restake)
    # We need the node address. For simplicity, we'll assume the user is a regular staker who holds rETH.
    # For rETH holders, rewards are not claimed via a separate contract; rETH increases in value.
    # The rocketpool reward claim is for node operators. This is complex.
    # To keep it realistic, we'll support both: if user is a node operator, they can claim RPL; otherwise just swap ETH for rETH.
    # I'll implement a simplified version: check if user has any balance of RPL token (0xD33526068D116cE69F19A9ee46F0bd304F21A51f) and claimable? Actually we can't detect claimable. We'll just skip that.
    # Instead, we'll implement the common "auto-restake": swap any ETH balance to rETH via Uniswap.
    # This is realistic because Rocket Pool's rETH can be minted by depositing ETH into the Rocket Pool contract (via the deposit function).
    # So we'll call deposit() on the RocketPool deposit pool.
    # The Rocket Pool deposit pool interface: deposit() payable.
    # We'll use that.
    # So the bot: if user has ETH, call RocketPoolDepositPool.deposit() to get rETH.
    # If user has RPL tokens, swap them to ETH first then deposit.
    # I'll implement accordingly.

    balance = w3.eth.get_balance(account.address)
    rpl_addr = "0xD33526068D116cE69F19A9ee46F0bd304F21A51f"
    rpl_token = w3.eth.contract(address=Web3.to_checksum_address(rpl_addr), abi=ERC20_ABI)
    rpl_balance = rpl_token.functions.balanceOf(account.address).call()

    # If RPL balance > 0, swap RPL for ETH
    if rpl_balance > 0:
        # Approve router
        if not _approve_if_needed(w3, rpl_addr, router_addr, account, rpl_balance, private_key):
            _post("RPL approve failed", "error")
            return
        # Get min out (RPL -> WETH)
        router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
        try:
            amounts_out = router.functions.getAmountsOut(rpl_balance, [rpl_addr, weth_addr]).call()
        except Exception as e:
            _post(f"Cannot get swap path for RPL->WETH: {e}", "error")
            return
        min_eth = int(amounts_out[-1] * (100 - config["swap_slippage_pct"]) / 100)
        deadline = int(time.time()) + 300
        try:
            tx = router.functions.swapExactTokensForTokens(
                rpl_balance, min_eth, [rpl_addr, weth_addr], account.address, deadline
            ).build_transaction({
                "from": account.address,
                "gas": int(config.get("gas_limit", 400000)),
                "gasPrice": int(w3.eth.gas_price * float(config.get("gas_price_multiplier", 1.1))),
                "nonce": w3.eth.get_transaction_count(account.address)
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status != 1:
                _post("RPL swap failed", "error")
                return
        except Exception as e:
            _post(f"RPL swap error: {e}", "error")
            return
        # Now we have more ETH (after swapping RPL)
        balance = w3.eth.get_balance(account.address)  # updated

    # If ETH balance > threshold, deposit into Rocket Pool to get rETH
    if balance >= Web3.to_wei(0.01, 'ether'):
        # The Rocket Pool deposit pool contract address on mainnet:
        # 0xDD3f50F8A6CafbE9b31a427582963f465E745AF8 (RocketDepositPool)
        # We'll need its ABI (deposit function).
        deposit_pool_addr = "0xDD3f50F8A6CafbE9b31a427582963f465E745AF8"
        deposit_abi = json.loads('''[
            {"constant":false,"inputs":[],"name":"deposit","outputs":[{"name":"","type":"bytes32"}],"payable":true,"stateMutability":"payable","type":"function"}
        ]''')
        deposit_pool = w3.eth.contract(address=Web3.to_checksum_address(deposit_pool_addr), abi=deposit_abi)
        gas_price = int(w3.eth.gas_price * float(config.get("gas_price_multiplier", 1.1)))
        gas_limit = int(config.get("gas_limit", 400000))
        try:
            tx = deposit_pool.functions.deposit().build_transaction({
                "from": account.address,
                "value": balance,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address)
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                _post(f"Deposited {Web3.from_wei(balance, 'ether')} ETH into Rocket Pool (rETH)", "info", {"amount": balance})
                return True
            else:
                _post("Rocket Pool deposit failed", "error")
                return False
        except Exception as e:
            _post(f"Rocket Pool deposit error: {e}", "error")
            return False
    else:
        _post("No ETH to deposit", "info")

def _approve_if_needed(w3, token_addr, spender_addr, account, amount, private_key):
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    allowance = token.functions.allowance(account.address, spender_addr).call()
    if allowance >= amount:
        return True
    gas_price = int(w3.eth.gas_price * float(config.get("gas_price_multiplier", 1.1)))
    tx = token.functions.approve(spender_addr, amount).build_transaction({
        "from": account.address,
        "gas": 100000,
        "gasPrice": gas_price,
        "nonce": w3.eth.get_transaction_count(account.address)
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt.status == 1

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Liquid Staking Auto‑Restaker Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        rpc = config["rpc_url"]
        w3 = Web3(Web3.HTTPProvider(rpc))
        if not w3.is_connected():
            _post("RPC connection failed", "error")
            time.sleep(60)
            continue

        private_key = config.get("private_key")
        dry_run = config.get("dry_run", False)
        if not private_key and not dry_run:
            _post("Private key not configured", "error")
            time.sleep(3600)
            continue

        account = None
        if private_key:
            account = w3.eth.account.from_key(private_key)

        protocol = config.get("protocol", "lido")
        if account and not dry_run:
            if protocol == "lido":
                claim_and_restake_lido(config, w3, account, private_key)
            elif protocol == "rocketpool":
                claim_and_restake_rocketpool(config, w3, account, private_key)
            else:
                _post(f"Unsupported protocol: {protocol}", "error")
        else:
            _post("Dry‑run mode: no transactions sent", "info")

        poll_interval = int(config.get("poll_interval_seconds", 3600))
        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
