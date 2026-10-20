#!/usr/bin/env python3
"""Early Token Scanner: research-only Solana and Binance Alpha discovery.

Uses Python's standard library. Does not trade, connect to wallets, or sign
transactions. Market-data APIs and schemas can change; verify every result.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("early-token-scanner")
STATE_PATH = Path(os.getenv("STATE_FILE", "data/early-token-scanner-state.json"))
DEX_BASE = "https://api.dexscreener.com"
BINANCE_BASE = "https://www.binance.com"
USER_AGENT = "EarlyTokenScanner/1.0 (research-only)"


def get_json(url: str, timeout: int = 20):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")
    except FileNotFoundError:
        state = {}
    except Exception as exc:
        LOG.warning("Unable to read saved state; starting fresh: %s", exc)
        state = {}
    state.setdefault("seen", {})
    state.setdefault("last_alert", {})
    state.setdefault("snapshots", {})
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def telegram_send(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID repository secrets.")
        return False

    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            LOG.error("Telegram API rejected the message.")
            return False
        return True
    except Exception as exc:
        LOG.error("Telegram delivery failed: %s", exc)
        return False


def fetch_solana_candidates():
    """Discover Solana token profiles, then inspect their reported DEX pairs."""
    try:
        profiles = get_json(f"{DEX_BASE}/token-profiles/latest/v1")
    except Exception as exc:
        LOG.warning("DEX Screener token-profile feed unavailable: %s", exc)
        return []

    if not isinstance(profiles, list):
        LOG.warning("Unexpected DEX Screener token-profile response.")
        return []

    candidates, processed = [], set()
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("chainId") != "solana":
            continue
        address = str(profile.get("tokenAddress") or "").strip()
        if not address or address in processed:
            continue
        processed.add(address)
        try:
            pairs = get_json(f"{DEX_BASE}/token-pairs/v1/solana/{quote(address, safe='')}")
        except Exception as exc:
            LOG.debug("Could not retrieve pairs for %s: %s", address, exc)
            continue
        if not isinstance(pairs, list):
            continue
        pairs = [p for p in pairs if isinstance(p, dict)]
        if not pairs:
            continue
        pairs.sort(key=lambda p: safe_float((p.get("liquidity") or {}).get("usd")), reverse=True)
        pair = pairs[0]
        base_token = pair.get("baseToken") or {}
        txns = pair.get("txns") or {}
        m5 = txns.get("m5") or {}
        h1 = txns.get("h1") or {}
        volume = pair.get("volume") or {}
        liquidity = pair.get("liquidity") or {}
        price_change = pair.get("priceChange") or {}
        candidates.append({
            "market": "solana",
            "address": address,
            "symbol": str(base_token.get("symbol") or "?")[:30],
            "name": str(base_token.get("name") or "Unknown")[:70],
            "url": pair.get("url") or f"https://dexscreener.com/solana/{pair.get('pairAddress', '')}",
            "pair_address": str(pair.get("pairAddress") or ""),
            "created_at_ms": pair.get("pairCreatedAt"),
            "price_usd": safe_float(pair.get("priceUsd")),
            "liquidity_usd": safe_float(liquidity.get("usd")),
            "volume_m5_usd": safe_float(volume.get("m5")),
            "volume_h1_usd": safe_float(volume.get("h1")),
            "buys_m5": int(safe_float(m5.get("buys"))),
            "sells_m5": int(safe_float(m5.get("sells"))),
            "buys_h1": int(safe_float(h1.get("buys"))),
            "sells_h1": int(safe_float(h1.get("sells"))),
            "price_change_h1_pct": safe_float(price_change.get("h1")),
            "fdv_usd": safe_float(pair.get("fdv")),
            "source": "DEX Screener",
        })
    return candidates


def fetch_binance_alpha_candidates():
    """Best-effort public Alpha token feed; Binance may change endpoint/schema."""
    endpoints = [
        f"{BINANCE_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list",
        f"{BINANCE_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/token/list",
    ]
    response_data = None
    for endpoint in endpoints:
        try:
            response_data = get_json(endpoint)
            if response_data:
                break
        except Exception as exc:
            LOG.info("Binance Alpha endpoint unavailable: %s", exc)

    if not isinstance(response_data, dict):
        return []
    payload = response_data.get("data")
    if isinstance(payload, dict):
        payload = payload.get("tokens") or payload.get("list") or payload.get("data")
    if not isinstance(payload, list):
        LOG.warning("Binance Alpha response format was not recognized; skipping this source.")
        return []

    candidates = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("tokenSymbol") or "?")[:30]
        name = str(item.get("name") or item.get("tokenName") or symbol)[:70]
        address = str(item.get("contractAddress") or item.get("address") or item.get("tokenAddress")
                      or item.get("alphaId") or item.get("id") or symbol).strip()
        candidates.append({
            "market": "binance_alpha",
            "address": address,
            "symbol": symbol,
            "name": name,
            "url": "https://www.binance.com/en/alpha",
            "pair_address": "",
            "created_at_ms": None,
            "price_usd": safe_float(item.get("price") or item.get("priceUsd")),
            "liquidity_usd": safe_float(item.get("liquidity") or item.get("liquidityUsd")),
            "volume_m5_usd": 0.0,
            "volume_h1_usd": safe_float(item.get("volume24h") or item.get("volume")),
            "buys_m5": 0,
            "sells_m5": 0,
            "buys_h1": 0,
            "sells_h1": 0,
            "price_change_h1_pct": safe_float(item.get("priceChange1h") or item.get("priceChangePercent")),
            "fdv_usd": safe_float(item.get("fdv") or item.get("fullyDilutedValuation")),
            "source": "Binance Alpha (best effort)",
        })
    return candidates


def token_key(candidate):
    return f"{candidate['market']}:{candidate['address']}".lower()


def risk_notes(candidate):
    notes = []
    if candidate["market"] == "solana":
        if candidate["liquidity_usd"] <= 0:
            notes.append("Liquidity missing or zero")
        elif candidate["liquidity_usd"] < 10000:
            notes.append("Low reported liquidity (under $10k)")
        if candidate["buys_h1"] + candidate["sells_h1"] == 0:
            notes.append("No recent transactions reported")
        notes.append("Mint/freeze authority and holder concentration are NOT audited by this scanner")
    else:
        notes.append("Alpha feed presence is not a contract-safety audit")
    return notes


def fmt_usd(value):
    if not value:
        return "unknown"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.5f}"


def make_alert(candidate, is_new, previous):
    reasons = []
    if is_new:
        reasons.append("First observed by this scanner (not necessarily newly launched)")
    if candidate["market"] == "solana":
        if candidate["buys_m5"] + candidate["sells_m5"] >= 5:
            reasons.append("At least five transactions reported in the last five minutes")
        prior_volume = safe_float(previous.get("volume_m5_usd"))
        if prior_volume > 0 and candidate["volume_m5_usd"] >= prior_volume * 1.5:
            reasons.append("Reported 5-minute volume is at least 1.5x the prior saved snapshot")
    else:
        reasons.append("Present in the available Binance Alpha feed; verify listing status")

    notes = risk_notes(candidate)
    lines = [
        "🔎 EARLY TOKEN RESEARCH ALERT",
        f"Market: {candidate['market'].replace('_', ' ').upper()}",
        f"Token: {candidate['name']} ({candidate['symbol']})",
        f"Address/ID: {candidate['address']}",
        f"Price: ${candidate['price_usd']:.10g}" if candidate["price_usd"] else "Price: unknown",
        f"Liquidity: {fmt_usd(candidate['liquidity_usd'])}",
        f"5m volume: {fmt_usd(candidate['volume_m5_usd'])}",
        f"1h volume: {fmt_usd(candidate['volume_h1_usd'])}",
        f"5m buys/sells: {candidate['buys_m5']}/{candidate['sells_m5']}",
        f"1h price change: {candidate['price_change_h1_pct']:.2f}%",
        f"FDV: {fmt_usd(candidate['fdv_usd'])}",
        "",
        "Why flagged:",
        *([f"• {reason}" for reason in reasons] or ["• Discovery feed match; not enough evidence to confirm momentum"]),
        "",
        "Risk notes:",
        *[f"⚠️ {note}" for note in notes],
        "",
        f"Chart/source: {candidate['url']}",
    ]
    if candidate["market"] == "solana":
        lines.append(f"Solscan: https://solscan.io/token/{candidate['address']}")
    lines.extend(["", "RESEARCH ONLY — not a buy signal. Data may be incomplete; verify independently."])
    return "\n".join(lines)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    state = load_state()
    candidates = fetch_solana_candidates() + fetch_binance_alpha_candidates()
    logging.info("Fetched %d candidates in total.", len(candidates))
    if not candidates:
        logging.warning("No candidates retrieved. APIs may be unavailable or their response formats may have changed.")

    now = int(time.time())
    cooldown = int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))
    max_alerts = int(os.getenv("MAX_ALERTS_PER_RUN", "5"))
    min_liquidity = float(os.getenv("MIN_SOLANA_LIQUIDITY_USD", "10000"))
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    sent_this_run = 0

    for candidate in candidates:
        key = token_key(candidate)
        is_new = key not in state["seen"]
        previous = state["snapshots"].get(key, {})
        last_alert = int(state["last_alert"].get(key, 0))

        if candidate["market"] == "solana":
            eligible = (
                candidate["liquidity_usd"] >= min_liquidity
                and (candidate["volume_m5_usd"] > 0 or candidate["volume_h1_usd"] > 0)
            )
        else:
            # Alpha endpoint payloads vary. Report candidates but clearly mark the source.
            eligible = True

        if eligible and (is_new or now - last_alert >= cooldown) and sent_this_run < max_alerts:
            message = make_alert(candidate, is_new, previous)
            if dry_run:
                logging.info("DRY RUN — no Telegram message sent:\n%s", message)
                state["last_alert"][key] = now
                sent_this_run += 1
            elif telegram_send(message):
                state["last_alert"][key] = now
                sent_this_run += 1

        state["seen"][key] = now
        state["snapshots"][key] = {
            "volume_m5_usd": candidate["volume_m5_usd"],
            "volume_h1_usd": candidate["volume_h1_usd"],
            "liquidity_usd": candidate["liquidity_usd"],
            "last_seen": now,
        }

    # Keep the state file bounded.
    if len(state["snapshots"]) > 5000:
        state["snapshots"] = dict(list(state["snapshots"].items())[-4000:])
    if len(state["seen"]) > 10000:
        state["seen"] = dict(list(state["seen"].items())[-8000:])
    save_state(state)
    logging.info("Scan complete; alerts processed this run: %d", sent_this_run)


if __name__ == "__main__":
    main()
