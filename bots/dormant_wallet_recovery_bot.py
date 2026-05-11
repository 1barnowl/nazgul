#!/usr/bin/env python3
"""
dormant_wallet_recovery_bot.py — Authorised Wallet Recovery Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans a list of authorised wallet addresses, attempts key
recovery from known weak passwords / mnemonics, checks the
blockchain for balances, and outputs signed transactions for
a 20% finder’s fee + 80% owner rescue.

⚠️  STRICTLY FOR AUTHORISED WALLET RECOVERY.
══════════════════════════════════════════════════
This bot must only be used on wallets you own or have
explicit, documented permission to recover. Use on any
wallet without authorisation is a felony in most jurisdictions.

SETUP
─────
1. Install dependencies:
      pip install bitcoinlib requests

2. Create a config file named `wallet_recovery_config.json`.
   Example at the bottom of this script.

3. (Optional) Set environment variables for broadcasting:
      BLOCKCYPHER_TOKEN="your-token"   (if you want to push raw tx)
      BROADCAST=true                    (if you want auto‑broadcast)

4. Attach to BotController.
"""

import json
import os
import time
import hashlib
import binascii
import threading
import requests
from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.transactions import Transaction

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "dormant_wallet_recovery_bot"
BOT_NAME = "Wallet Recovery Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wallet_recovery_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "wallet_recovery_state.json")

SCAN_INTERVAL      = 43200   # 12 hours – recovery attempts are slow
HEARTBEAT_INTERVAL = 300
_last_hb = 0.0
_last_hb_lock = threading.Lock()

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
    with _last_hb_lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
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
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "fee_address": "bc1q...your_fee_address...",
            "rescue_address": "bc1q...owner_rescue_address...",
            "wallets": [
                {
                    "address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                    "description": "User A – forgot password",
                    "candidates": [
                        "password123", "bitcoin2010", "satoshinakamoto"
                    ]
                }
            ],
            "attempt_mnemonic_variations": False,
            "broadcast": False
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State (prevents repeated attempts on recovered wallets) ─────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"recovered": {}}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Blockchain balance checker ─────────────────────────────────────────────────
def get_btc_balance(address):
    """Query blockchain.info API for balance in BTC."""
    try:
        resp = requests.get(f"https://blockchain.info/q/addressbalance/{address}?confirmations=6", timeout=15)
        if resp.status_code == 200:
            satoshis = int(resp.text)
            return satoshis / 1e8
    except Exception as e:
        _post(f"Balance check error for {address}: {e}", "warning")
    return None

# ── Key derivation from password / mnemonic ────────────────────────────────────
def attempt_private_key(candidate, target_address):
    """
    Try to produce a private key that matches target_address using:
      - WIF import (if candidate is a WIF string)
      - BIP39 mnemonic → seed → BIP84 derivation (if looks like phrase)
      - Simple SHA256(password) as a brain wallet (legacy)
    Returns (privkey_hex, derived_address) or None.
    """
    # 1. Try WIF
    try:
        from bitcoinlib.keys import Key
        k = Key(import_key=candidate, network='bitcoin')
        if k.address() == target_address:
            return k.private_hex, k.address()
    except:
        pass

    # 2. Try BIP39 mnemonic (if candidate is a space-separated phrase)
    if ' ' in candidate:
        try:
            seed = Mnemonic().to_seed(candidate, password='')
            hdkey = HDKey.from_seed(seed, network='bitcoin')
            # Derive standard BIP84 native SegWit (m/84'/0'/0'/0/0)
            child = hdkey.subkey_for_path("m/84'/0'/0'/0/0")
            if child.address() == target_address:
                return child.private_hex, child.address()
            # Also try legacy m/44'/0'/0'/0/0
            child_legacy = hdkey.subkey_for_path("m/44'/0'/0'/0/0")
            if child_legacy.address() == target_address:
                return child_legacy.private_hex, child_legacy.address()
        except:
            pass

    # 3. Try brain wallet (SHA256 of password)
    try:
        privkey_hex = hashlib.sha256(candidate.encode()).hexdigest()
        k = Key(import_key=privkey_hex, network='bitcoin')
        if k.address() == target_address:
            return k.private_hex, k.address()
    except:
        pass

    return None

# ── Transaction builder (20% fee, 80% rescue) ──────────────────────────────────
def build_fee_transaction(privkey_hex, from_address, satoshi_balance, fee_address, rescue_address):
    """
    Construct a raw Bitcoin transaction that sends 20% to fee_address,
    80% to rescue_address, and a small miner fee.
    Returns signed raw hex or None.
    """
    try:
        from bitcoinlib.keys import Key
        from bitcoinlib.transactions import Transaction, Output

        k = Key(import_key=privkey_hex, network='bitcoin')

        # Estimate fee (very rough – use a fee service in production)
        fee = 5000  # satoshis – adjust as needed
        total = satoshi_balance

        # Amounts (must be integer satoshis)
        fee_amt = int(total * 0.2)
        rescue_amt = total - fee_amt - fee
        if rescue_amt <= 0:
            _post("Balance too small after fee split.", "warning")
            return None

        # We need UTXOs of the address – query the API
        utxos = _get_utxos(from_address)
        if not utxos or sum(utxo['value'] for utxo in utxos) < total:
            _post(f"Insufficient confirmed UTXOs for {from_address}", "warning")
            return None

        t = Transaction(network='bitcoin', fee=fee)
        # Add inputs
        for utxo in utxos:
            inp = t.add_input(prev_txid=utxo['txid'], output_n=utxo['vout'])
        # Add outputs
        t.add_output(Output(value=fee_amt, address=fee_address))
        t.add_output(Output(value=rescue_amt, address=rescue_address))

        t.sign(k)
        return t.raw_hex()
    except Exception as e:
        _post(f"Transaction build error: {e}", "error")
        return None

def _get_utxos(address):
    """Return list of UTXOs: [{'txid': ..., 'vout': ..., 'value': satoshi}]."""
    try:
        resp = requests.get(f"https://blockchain.info/unspent?active={address}&limit=50", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('unspent_outputs', [])
    except Exception as e:
        _post(f"UTXO fetch error: {e}", "warning")
    return []

# ── Main recovery scan ─────────────────────────────────────────────────────────
def scan():
    state = load_state()
    wallets = CFG.get("wallets", [])

    for wallet in wallets:
        address = wallet["address"]
        if state.get("recovered", {}).get(address):
            continue  # already processed

        balance = get_btc_balance(address)
        if balance is None or balance < 0.0001:  # ignore dust
            continue

        _post(f"Checking {address} ({balance:.8f} BTC)...", "info")

        recovered_key = None
        for candidate in wallet.get("candidates", []):
            _post(f"Trying candidate: '{candidate}'", "info")
            result = attempt_private_key(candidate, address)
            if result:
                privkey, _ = result
                recovered_key = privkey
                break

        if recovered_key:
            _post(f"PRIVATE KEY RECOVERED for {address} with password: {candidate}", "error")
            # Build transaction
            fee_addr = CFG.get("fee_address", "")
            rescue_addr = CFG.get("rescue_address", "")
            satoshi_balance = int(balance * 1e8)
            raw_tx = build_fee_transaction(recovered_key, address, satoshi_balance, fee_addr, rescue_addr)
            if raw_tx:
                _post(
                    f"RECOVERED {balance:.8f} BTC from {address}\n"
                    f"  TX: {raw_tx[:80]}...\n"
                    f"  Fee (20%): {fee_addr} / Rescue (80%): {rescue_addr}",
                    "error",
                    {"raw_tx": raw_tx}
                )
                # Optionally broadcast
                if CFG.get("broadcast") and os.getenv("BLOCKCYPHER_TOKEN"):
                    broadcast_tx(raw_tx)
            state.setdefault("recovered", {})[address] = True
            save_state(state)
        else:
            _post(f"No working password found for {address}", "info")

def broadcast_tx(raw_hex):
    """Push raw transaction to the Bitcoin network via BlockCypher (if token set)."""
    token = os.getenv("BLOCKCYPHER_TOKEN", "")
    if not token:
        _post("No BLOCKCYPHER_TOKEN – cannot broadcast.", "warning")
        return
    try:
        resp = requests.post(
            "https://api.blockcypher.com/v1/btc/main/txs/push",
            json={"tx": raw_hex},
            params={"token": token},
            timeout=15
        )
        if resp.status_code in (200, 201):
            _post("Transaction broadcast successfully!", "info")
        else:
            _post(f"Broadcast failed: {resp.status_code} {resp.text}", "error")
    except Exception as e:
        _post(f"Broadcast error: {e}", "error")

def main():
    _wait_for_hub()
    _post("Authorised Wallet Recovery Bot online. Scanning configured wallets.", "info")

    while True:
        scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `wallet_recovery_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "fee_address": "bc1q...your-finder-fee-address...",
  "rescue_address": "bc1q...owner-recovery-address...",
  "wallets": [
    {
      "address": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
      "description": "Lost wallet of old friend",
      "candidates": ["correct horse battery staple", "123456", "bitcoin"]
    },
    {
      "address": "bc1q...another-wallet...",
      "description": "Own cold storage with forgotten passphrase",
      "candidates": ["myfavoritedog", "summer2020", "giottus"]
    }
  ],
  "attempt_mnemonic_variations": false,
  "broadcast": false
}
"""
