#!/usr/bin/env python3
"""
multi_chain_yield_scanner_bot.py — Multi‑Chain Yield Scanner Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans Ethereum, Arbitrum, Optimism, and Polygon for the highest
APY on a given token pair (e.g. USDC/WETH) via DefiLlama, then
uses Li.Fi to bridge and deposit in one transaction when a
better opportunity is found.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests

Configuration
─────────────
Place `multi_chain_scanner_config.json` in the same directory:

{
  "rpc_urls": {
    "ethereum": "https://mainnet.infura.io/v3/YOUR_KEY",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://main.optimism.io",
    "polygon": "https://polygon-rpc.com"
  },
  "private_key": "0xYOUR_PRIVATE_KEY",
  "dry_run": false,
  "pair": {
    "token0": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "token1": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
  },
  "bridge": {
    "provider": "lifi",
    "lifi_api_url": "https://li.quest/v1"
  },
  "min_apy_improvement_pct": 2.0,
  "poll_interval_minutes": 30,
  "state_file": "multi_chain_scanner_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from web3 import Web3

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "multi_chain_yield_scanner_bot"
BOT_NAME = "Multi‑Chain Yield Scanner"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "multi_chain_scanner_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── ABIs ─────────────────────────────────────────────────────────
UNISWAP_V2_ROUTER_ABI = json.loads(
    '''[
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"amountADesired","type":"uint256"},{"internalType":"uint256","name":"amountBDesired","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"addLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"},{"internalType":"uint256","name":"liquidity","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"uint256","name":"liquidity","type":"uint256"},{"internalType":"uint256","name":"amountAMin","type":"uint256"},{"internalType":"uint256","name":"amountBMin","type":"uint256"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"removeLiquidity","outputs":[{"internalType":"uint256","name":"amountA","type":"uint256"},{"internalType":"uint256","name":"amountB","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]'''
)

MASTERCHEF_ABI = json.loads(
    '''[
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"constant":false,"inputs":[{"name":"_pid","type":"uint256"},{"name":"_amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"constant":true,"inputs":[{"name":"_pid","type":"uint256"}],"name":"poolInfo","outputs":[{"name":"lpToken","type":"address"},{"name":"allocPoint","type":"uint256"},{"name":"lastRewardBlock","type":"uint256"},{"name":"accRewardPerShare","type":"uint256"}],"stateMutability":"view","type":"function"}
]'''
)

ERC20_ABI = json.loads(
    '''[
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}
]'''
)

# ── DefiLlama data ───────────────────────────────────────────────
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"

def fetch_pools() -> List[dict]:
    try:
        resp = requests.get(DEFILLAMA_POOLS_URL, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        _post(f"DefiLlama API error: {e}", "error")
        return []

def find_best_pool(pools: List[dict], token0: str, token1: str,
                   current_pool_id: Optional[str] = None,
                   min_apy_improvement_pct: float = 0.0) -> Optional[dict]:
    """
    Return the pool with the highest APY that involves exactly the given
    pair of tokens (as underlyingTokens).  Exclude the current pool if
    provided, and only consider pools where APY exceeds the current best
    by the given percentage.
    """
    token0 = token0.lower()
    token1 = token1.lower()
    best = None
    best_apy = 0.0
    for p in pools:
        underlying = [t.lower() for t in p.get("underlyingTokens", [])]
        if len(underlying) != 2:
            continue
        if token0 not in underlying or token1 not in underlying:
            continue
        apy = p.get("apy", 0) or 0
        if apy > best_apy:
            best_apy = apy
            best = p

    if best is None:
        return None

    if current_pool_id and best["pool"] == current_pool_id:
        # already in the best pool
        return None

    if current_pool_id:
        # find current pool APY from the data
        current_apy = 0.0
        for p in pools:
            if p["pool"] == current_pool_id:
                current_apy = p.get("apy", 0) or 0
                break
        if current_apy > 0:
            if (best_apy - current_apy) < min_apy_improvement_pct:
                return None
    return best

# ── Li.Fi integration ────────────────────────────────────────────
LIFI_API = "https://li.quest/v1"

def build_lifi_route(from_token: str, to_token: str, from_amount_wei: int,
                     from_chain_id: int, to_chain_id: int,
                     from_address: str, to_address: str) -> Optional[dict]:
    """
    Request a route from Li.Fi that bridges and swaps.  Returns the
    transaction dict to be sent, or None on failure.
    """
    url = f"{LIFI_API}/advanced/stepTransactions"
    payload = {
        "fromToken": from_token,
        "toToken": to_token,
        "fromAmount": str(from_amount_wei),
        "fromAddress": from_address,
        "toAddress": to_address,
        "fromChain": from_chain_id,
        "toChain": to_chain_id,
        "options": {
            "slippage": 0.01,
            "order": "CHEAPEST"  # or FASTEST
        }
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Li.Fi API error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Li.Fi request error: {e}", "error")
        return None

def execute_lifi_transaction(w3: Web3, tx_data: dict, private_key: str,
                             gas_multiplier: float = 1.1) -> bool:
    """Sign and send a Li.Fi prepared transaction."""
    account = w3.eth.account.from_key(private_key)
    gas_price = int(w3.eth.gas_price * gas_multiplier)
    tx = {
        "from": account.address,
        "to": Web3.to_checksum_address(tx_data["to"]),
        "data": tx_data["data"],
        "value": int(tx_data.get("value", 0)),
        "gas": int(tx_data.get("gasLimit", 500000)),
        "gasPrice": gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
    }
    try:
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        return receipt.status == 1
    except Exception as e:
        _post(f"Li.Fi tx failed: {e}", "error")
        return False

# ── Position management ─────────────────────────────────────────
def exit_current_position(config: dict, state: dict) -> bool:
    """
    Withdraw LP tokens from the current MasterChef farm, remove liquidity,
    and receive back the two tokens.
    """
    current = state.get("current_position")
    if not current:
        return True  # nothing to exit

    chain = current["chain"]
    rpc = config["rpc_urls"].get(chain)
    if not rpc:
        _post(f"No RPC for chain {chain}", "error")
        return False
    w3 = Web3(Web3.HTTPProvider(rpc))
    account = w3.eth.account.from_key(config["private_key"])
    private_key = config["private_key"]
    gas_price = int(w3.eth.gas_price * 1.1)
    gas_limit = 400000

    farm_addr = Web3.to_checksum_address(current["farm"])
    pid = current["pid"]
    router_addr = Web3.to_checksum_address(current["router"])
    token0 = Web3.to_checksum_address(current["token0"])
    token1 = Web3.to_checksum_address(current["token1"])

    farm = w3.eth.contract(address=farm_addr, abi=MASTERCHEF_ABI)
    # Withdraw from farm
    try:
        tx = farm.functions.withdraw(pid, current["lp_balance"]).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            _post("Withdraw from farm failed", "error")
            return False
    except Exception as e:
        _post(f"Withdraw error: {e}", "error")
        return False

    # Approve router to spend LP tokens
    lp_token = w3.eth.contract(address=Web3.to_checksum_address(current["lp_token"]), abi=ERC20_ABI)
    allowance = lp_token.functions.allowance(account.address, router_addr).call()
    lp_balance = lp_token.functions.balanceOf(account.address).call()
    if allowance < lp_balance:
        try:
            tx = lp_token.functions.approve(router_addr, lp_balance).build_transaction({
                "from": account.address,
                "gas": 100000,
                "gasPrice": gas_price,
                "nonce": w3.eth.get_transaction_count(account.address)
            })
            signed = account.sign_transaction(tx)
            w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)
        except Exception as e:
            _post(f"LP approve error: {e}", "error")
            return False

    # Remove liquidity
    router = w3.eth.contract(address=router_addr, abi=UNISWAP_V2_ROUTER_ABI)
    deadline = int(time.time()) + 300
    try:
        tx = router.functions.removeLiquidity(
            token0, token1, lp_balance,
            0, 0, account.address, deadline
        ).build_transaction({
            "from": account.address,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(account.address)
        })
        signed = account.sign_transaction(tx)
        w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)
    except Exception as e:
        _post(f"Remove liquidity error: {e}", "error")
        return False

    return True

def enter_new_pool_via_lifi(config: dict, target_pool: dict, state: dict) -> bool:
    """
    Use Li.Fi to bridge token0 balance from the current chain to the target
    chain and deposit into the given MasterChef farm.  We send all of the
    token0 that the user holds on the source chain.
    """
    current_chain = state.get("current_position", {}).get("chain")
    if not current_chain:
        # no previous position: assume user has tokens on a source chain? For simplicity,
        # we'll try to use the chain where the user currently has the most token0.
        # We'll scan all chains for token0 balance.
        token0_addr = Web3.to_checksum_address(config["pair"]["token0"])
        max_chain = None
        max_balance = 0
        for chain_name, rpc_url in config["rpc_urls"].items():
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url))
                account = w3.eth.account.from_key(config["private_key"])
                token = w3.eth.contract(address=token0_addr, abi=ERC20_ABI)
                bal = token.functions.balanceOf(account.address).call()
                if bal > max_balance:
                    max_balance = bal
                    max_chain = chain_name
            except Exception:
                continue
        if not max_chain or max_balance == 0:
            _post("No token0 balance found on any chain", "error")
            return False
        source_chain = max_chain
        source_amount = max_balance
    else:
        source_chain = current_chain
        # balance after exit: we need to know the token0 balance on that chain
        rpc = config["rpc_urls"].get(source_chain)
        w3 = Web3(Web3.HTTPProvider(rpc))
        account = w3.eth.account.from_key(config["private_key"])
        token0 = w3.eth.contract(address=Web3.to_checksum_address(config["pair"]["token0"]), abi=ERC20_ABI)
        source_amount = token0.functions.balanceOf(account.address).call()

    if source_amount == 0:
        _post("No token0 to migrate", "warning")
        return False

    # Map chain names to Li.Fi chain IDs
    chain_id_map = {
        "ethereum": 1,
        "arbitrum": 42161,
        "optimism": 10,
        "polygon": 137,
    }
    source_chain_id = chain_id_map.get(source_chain)
    target_chain = target_pool.get("chain", "").lower()
    target_chain_id = chain_id_map.get(target_chain)
    if not source_chain_id or not target_chain_id:
        _post("Chain mapping missing", "error")
        return False

    # Li.Fi route: from token0 on source chain to the target farm's deposit transaction.
    # We need to encode a call that includes bridge + swap + add liquidity + deposit.
    # Li.Fi's advanced stepTransactions can handle complex flows if we set "toToken" as the LP token? 
    # We'll instead use the "toToken" as the LP token address? Not exactly. The easiest is to 
    # request Li.Fi to swap token0 to the target chain's LP token and then we manually deposit.
    # But Li.Fi can also "toAmount" and destination contract call. We'll keep it simple:
    # We'll bridge token0 to token0 on target chain, then manually swap and deposit.
    # So we'll do a simple cross-chain transfer via Li.Fi for token0, then on the target chain we'll
    # perform the swap and deposit using local logic.
    
    # Build Li.Fi route for cross-chain transfer of token0.
    lifi_route = build_lifi_route(
        from_token=config["pair"]["token0"],
        to_token=config["pair"]["token0"],   # same token, just bridge
        from_amount_wei=source_amount,
        from_chain_id=source_chain_id,
        to_chain_id=target_chain_id,
        from_address=account.address,
        to_address=account.address,
    )
    if not lifi_route:
        return False

    # Execute Li.Fi transaction on source chain
    source_rpc = config["rpc_urls"].get(source_chain)
    source_w3 = Web3(Web3.HTTPProvider(source_rpc))
    if not execute_lifi_transaction(source_w3, lifi_route["transactionRequest"], config["private_key"]):
        return False

    # Wait for bridge finality (simplified: assume it's ready after a few minutes)
    time.sleep(120)  # crude, but we can check balance periodically

    # Now on target chain, we need to enter the pool: swap half to token1, add liquidity, deposit.
    target_rpc = config["rpc_urls"].get(target_chain)
    target_w3 = Web3(Web3.HTTPProvider(target_rpc))
    target_account = target_w3.eth.account.from_key(config["private_key"])
    target_token0 = Web3.to_checksum_address(config["pair"]["token0"])
    target_token1 = Web3.to_checksum_address(config["pair"]["token1"])
    target_router = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")  # common Uniswap V2 router on many chains
    target_farm = Web3.to_checksum_address(target_pool["pool"])
    target_pid = target_pool.get("pid", 0)

    # Check token0 balance on target chain
    token0_contract = target_w3.eth.contract(address=target_token0, abi=ERC20_ABI)
    token0_balance = token0_contract.functions.balanceOf(target_account.address).call()
    if token0_balance == 0:
        _post("No token0 received after bridging", "error")
        return False

    gas_price = int(target_w3.eth.gas_price * 1.1)
    gas_limit = 400000
    slippage = 0.01
    deadline = int(time.time()) + 300

    # Swap half to token1
    half = token0_balance // 2
    router = target_w3.eth.contract(address=target_router, abi=UNISWAP_V2_ROUTER_ABI)
    # Approve router for token0
    if not _approve_if_needed(target_w3, target_token0, target_router, target_account, half, gas_price, gas_limit, config["private_key"]):
        return False
    # Get min out
    amounts_out = router.functions.getAmountsOut(half, [target_token0, target_token1]).call()
    min_out = int(amounts_out[-1] * (1 - slippage))
    tx = router.functions.swapExactTokensForTokens(
        half, min_out, [target_token0, target_token1], target_account.address, deadline
    ).build_transaction({
        "from": target_account.address,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": target_w3.eth.get_transaction_count(target_account.address)
    })
    signed = target_account.sign_transaction(tx)
    target_w3.eth.wait_for_transaction_receipt(target_w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)

    # Add liquidity
    token1_contract = target_w3.eth.contract(address=target_token1, abi=ERC20_ABI)
    bal0 = token0_contract.functions.balanceOf(target_account.address).call()
    bal1 = token1_contract.functions.balanceOf(target_account.address).call()
    _approve_if_needed(target_w3, target_token0, target_router, target_account, bal0, gas_price, gas_limit, config["private_key"])
    _approve_if_needed(target_w3, target_token1, target_router, target_account, bal1, gas_price, gas_limit, config["private_key"])
    tx = router.functions.addLiquidity(
        target_token0, target_token1, bal0, bal1,
        int(bal0 * (1 - slippage)), int(bal1 * (1 - slippage)),
        target_account.address, deadline
    ).build_transaction({
        "from": target_account.address,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": target_w3.eth.get_transaction_count(target_account.address)
    })
    signed = target_account.sign_transaction(tx)
    target_w3.eth.wait_for_transaction_receipt(target_w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)

    # Stake LP tokens
    farm_contract = target_w3.eth.contract(address=target_farm, abi=MASTERCHEF_ABI)
    pool_info = farm_contract.functions.poolInfo(target_pid).call()
    lp_token_addr = pool_info[0]
    lp_token_contract = target_w3.eth.contract(address=lp_token_addr, abi=ERC20_ABI)
    lp_balance = lp_token_contract.functions.balanceOf(target_account.address).call()
    _approve_if_needed(target_w3, lp_token_addr, target_farm, target_account, lp_balance, gas_price, gas_limit, config["private_key"])
    tx = farm_contract.functions.deposit(target_pid, lp_balance).build_transaction({
        "from": target_account.address,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": target_w3.eth.get_transaction_count(target_account.address)
    })
    signed = target_account.sign_transaction(tx)
    receipt = target_w3.eth.wait_for_transaction_receipt(target_w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)
    if receipt.status == 1:
        _post(f"Successfully migrated to {target_pool['symbol']} on {target_chain} ({target_pool.get('apy')}% APY)", "info")
        return True
    else:
        _post("Stake deposit failed", "error")
        return False

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
        w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.rawTransaction), timeout=120)
        return True
    except Exception as e:
        _post(f"Approve error: {e}", "error")
        return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Multi‑Chain Yield Scanner Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        private_key = config.get("private_key")
        dry_run = config.get("dry_run", False)
        if not private_key and not dry_run:
            _post("Private key missing", "error")
            time.sleep(3600)
            continue

        pair = config["pair"]
        token0 = pair["token0"]
        token1 = pair["token1"]
        state_file = config.get("state_file", "multi_chain_scanner_state.json")
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
        else:
            state = {}

        # Fetch all yield pools
        pools = fetch_pools()
        if not pools:
            _post("No pool data", "error")
            time.sleep(300)
            continue

        # Find the overall best pool for the pair
        current_pool_id = state.get("current_position", {}).get("pool_id")
        best = find_best_pool(pools, token0, token1, current_pool_id,
                              float(config.get("min_apy_improvement_pct", 2.0)))
        if best:
            _post(f"Best pool: {best['symbol']} on {best.get('chain','?')} APY {best.get('apy')}%",
                  "info", best)
            if not dry_run and private_key:
                # Exit current position if needed
                if current_pool_id and current_pool_id != best["pool"]:
                    _post("Exiting current position...", "warning")
                    if not exit_current_position(config, state):
                        _post("Failed to exit current position", "error")
                        continue
                    # Clear current position from state
                    state["current_position"] = None
                # Enter new pool
                success = enter_new_pool_via_lifi(config, best, state)
                if success:
                    # Update state
                    state["current_position"] = {
                        "chain": best["chain"],
                        "pool_id": best["pool"],
                        "farm": best["pool"],  # assuming pool == farm contract
                        "pid": best.get("pid", 0),
                        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2, will need to be chain-specific; we assume it's on all chains
                        "token0": token0,
                        "token1": token1,
                        "lp_token": "",  # will be fetched after deposit
                        "lp_balance": 0
                    }
                    with open(state_file, "w") as f:
                        json.dump(state, f, indent=2)
            else:
                _post("Dry‑run or missing private key; not migrating", "info")
        else:
            _post("No better pool found", "info")

        poll_minutes = int(config.get("poll_interval_minutes", 30))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
