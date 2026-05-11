#!/usr/bin/env python3
"""
liquidation_scanner_bot.py — DeFi Liquidation Opportunity Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors Aave V3 and Compound V3 on Ethereum for under‑collateralised
borrow positions. Alerts when a position is liquidatable or close.

SETUP
─────
1. Install dependencies:
      pip install web3 requests

2. Export an Ethereum RPC URL (HTTPS is fine for reading):
      export ETH_RPC_URL="https://mainnet.infura.io/v3/YOUR-KEY"

3. Attach to BotController.
"""

import os
import json
import time
import requests
from web3 import Web3

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "liquidation_scanner_bot"
BOT_NAME = "Liquidation Scanner"

HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except Exception:
        pass

def _heartbeat():
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    _last_hb = time.time()
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Configuration ──────────────────────────────────────────────────────────────
ETH_RPC_URL = os.getenv("ETH_RPC_URL", "").strip()
if not ETH_RPC_URL:
    _post("ETH_RPC_URL not set. Bot idle.", "error")

# Minimum health factor to consider as "at risk" (e.g. 1.05 = early warning)
WARN_HEALTH_FACTOR = 1.05

# ── Blockchain connection ──────────────────────────────────────────────────────
w3 = None
if ETH_RPC_URL:
    w3 = Web3(Web3.HTTPProvider(ETH_RPC_URL))
    if not w3.is_connected():
        _post("Cannot connect to Ethereum RPC.", "error")
        w3 = None

# ── Aave V3 Pool contract (mainnet) ────────────────────────────────────────────
AAVE_V3_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
AAVE_V3_POOL_ABI = json.loads("""[
    {
        "inputs": [
            {"internalType": "address","name": "user","type": "address"},
            {"internalType": "uint256","name": "reserveIndex","type": "uint256"}
        ],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256","name": "totalCollateralBase","type": "uint256"},
            {"internalType": "uint256","name": "totalDebtBase","type": "uint256"},
            {"internalType": "uint256","name": "availableBorrowsBase","type": "uint256"},
            {"internalType": "uint256","name": "currentLiquidationThreshold","type": "uint256"},
            {"internalType": "uint256","name": "ltv","type": "uint256"},
            {"internalType": "uint256","name": "healthFactor","type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# ── Compound V3 Comet contract (USDC market on mainnet) ───────────────────────
COMPOUND_V3_USDC = "0xc3d688B66703497DAA19211EEdf47f25384cdc3c"
COMPOUND_V3_ABI = json.loads("""[
    {
        "inputs": [{"internalType": "address","name": "account","type": "address"}],
        "name": "userCollateral",
        "outputs": [
            {"internalType": "uint128","name": "balance","type": "uint128"},
            {"internalType": "bool","name": "_inCollateral","type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address","name": "account","type": "address"}],
        "name": "borrowBalanceOf",
        "outputs": [{"internalType": "uint256","name":"","type":"uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getUtilization",
        "outputs": [{"internalType": "uint","name":"","type":"uint"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

# ── Scanning logic ─────────────────────────────────────────────────────────────

def scan_aave():
    """Check a predefined list of known borrowers (or you can iterate over events)."""
    # For demonstration, we use a small list of addresses that have active loans.
    # In a production bot you would fetch recent Borrow events or use a subgraph.
    known_borrowers = [
        "0x...",  # add addresses to monitor
        "0x...",
    ]
    if not known_borrowers:
        _post("No Aave borrower addresses configured. Add some in the code.", "info")
        return

    pool = w3.eth.contract(address=Web3.to_checksum_address(AAVE_V3_POOL), abi=AAVE_V3_POOL_ABI)
    for user in known_borrowers:
        try:
            data = pool.functions.getUserAccountData(Web3.to_checksum_address(user)).call()
            health_factor = data[5] / 1e18
            if health_factor <= WARN_HEALTH_FACTOR:
                level = "error" if health_factor < 1.0 else "warning"
                _post(
                    f"Aave V3: {user[:10]}... health factor {health_factor:.4f} {'LIQUIDATABLE' if health_factor < 1 else 'at risk'}",
                    level,
                    {"protocol": "Aave V3", "user": user, "health_factor": health_factor}
                )
        except Exception as e:
            _post(f"Error reading Aave user {user}: {e}", "warning")

def scan_compound():
    """Check a small list of known Compound borrowers."""
    known_borrowers = [
        "0x...",
    ]
    if not known_borrowers:
        _post("No Compound borrower addresses configured.", "info")
        return

    comet = w3.eth.contract(address=Web3.to_checksum_address(COMPOUND_V3_USDC), abi=COMPOUND_V3_ABI)
    for user in known_borrowers:
        try:
            collateral = comet.functions.userCollateral(Web3.to_checksum_address(user)).call()
            borrow_balance = comet.functions.borrowBalanceOf(Web3.to_checksum_address(user)).call()
            # Compound V3 liquidatable if borrowBalance > (collateral.balance * collateralFactor)
            # We approximate: if borrowBalance > collateral.balance * 0.8 (typical factor)
            # Better: use getUtilization or fetch liquidationFactor from the comet configuration.
            # Here we use a simple check: if borrowBalance > 0 and collateral.balance == 0
            if borrow_balance > 0 and collateral[0] == 0:
                _post(
                    f"Compound V3: {user[:10]}... borrow {borrow_balance / 1e6:.2f} USDC, zero collateral – LIQUIDATABLE!",
                    "error",
                    {"protocol": "Compound V3", "user": user, "borrow_usdc": borrow_balance / 1e6}
                )
        except Exception as e:
            _post(f"Error reading Compound user {user}: {e}", "warning")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    if not ETH_RPC_URL or not w3:
        _post("Ethereum RPC unavailable. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post("Liquidation Scanner online. Monitoring Aave V3 & Compound V3.", "info")

    while True:
        scan_aave()
        scan_compound()
        _heartbeat()
        time.sleep(60)  # scan every minute

if __name__ == "__main__":
    main()
