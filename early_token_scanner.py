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
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("early-token-scanner")
STATE_PATH = Path(os.getenv("STATE_FILE", "data/early-token-scanner-state.json"))
DEX_BASE = "https://api.dexscreener.com"
USER_AGENT = "EarlyTokenScanner/1.1 (research-only)"


def get_json(url: str, timeout: int = 20, headers=None):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first_value(item, *keys):
    """Return the first non-empty value from a dictionary."""
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


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
            LOG.error("Telegram API rejected the message: %s", result.get("description", "unknown error"))
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
    LOG.info("Solana discovery: %d candidates.", len(candidates))
    return candidates


def extract_binance_token_list(response_data):
    """Tolerate known direct and nested Binance response shapes."""
    if isinstance(response_data, list):
        return response_data
    if not isinstance(response_data, dict):
        return None

    code = response_data.get("code")
    if code not in (None, "", "000000", 0, "0"):
        LOG.warning(
            "Binance Alpha API returned code=%s message=%s",
            code,
            response_data.get("message") or response_data.get("msg") or "unknown",
        )
        return None
    if response_data.get("success") is False:
        LOG.warning(
            "Binance Alpha API reported failure: %s",
            response_data.get("message") or response_data.get("msg") or "unknown",
        )
        return None

    data = response_data.get("data")
    for _ in range(5):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return None
        next_data = None
        for key in ("tokens", "list", "data", "tokenList", "alphaTokenList", "items"):
            if isinstance(data.get(key), (dict, list)):
                next_data = data[key]
                break
        if next_data is None:
            return None
        data = next_data
    return data if isinstance(data, list) else None


def fetch_binance_alpha_candidates():
    """Fetch Binance Alpha token-list data and normalize it for alerts.

    Binance can change public endpoints and response fields. Every failure is
    logged so a zero-candidate run can be diagnosed from GitHub Actions logs.
    """
    endpoints = [
        "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list",
        "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/token/list",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.binance.com/en/alpha",
        "Origin": "https://www.binance.com",
    }

    payload = None
    for endpoint in endpoints:
        try:
            result = get_json(endpoint, timeout=25, headers=headers)
            payload = extract_binance_token_list(result)
            if payload is not None:
                LOG.info(
                    "Binance Alpha endpoint returned %d token records: %s",
                    len(payload),
                    endpoint,
                )
                break
            LOG.warning(
                "Binance Alpha response schema not recognized at %s; top-level keys=%s",
                endpoint,
                list(result.keys()) if isinstance(result, dict) else type(result).__name__,
            )
        except HTTPError as exc:
            LOG.warning("Binance Alpha HTTP error at %s: %s", endpoint, exc)
        except URLError as exc:
            LOG.warning("Binance Alpha network error at %s: %s", endpoint, exc)
        except Exception as exc:
            LOG.warning("Binance Alpha request failed at %s: %s", endpoint, exc)

    if payload is None:
        LOG.error("Binance Alpha discovery failed on all configured endpoints.")
        return []

    candidates = []
    skipped = 0
    for item in payload:
        if not isinstance(item, dict):
            skipped += 1
            continue

        symbol = str(first_value(item, "symbol", "tokenSymbol", "ticker") or "").strip()[:30]
        name = str(first_value(item, "name", "tokenName", "fullName") or symbol or "Unknown").strip()[:70]
        alpha_id = str(first_value(item, "alphaId", "alphaTokenId") or "").strip()
        contract = str(first_value(item, "contractAddress", "address", "tokenAddress", "contract") or "").strip()
        token_id = str(first_value(item, "tokenId", "id") or "").strip()
        address = contract or alpha_id or token_id

        if not address:
            skipped += 1
            continue

        # Field names vary between token-list versions; don't invent values.
        price = safe_float(first_value(item, "price", "priceUsd", "priceUSD"))
        volume_24h = safe_float(first_value(
            item, "volume24h", "volume24H", "volume", "volumeUsd", "volumeUSD", "tradingVolume"
        ))
        price_change_24h = safe_float(first_value(
            item, "percentChange24h", "priceChange24h", "priceChangePercent", "priceChange"
        ))
        fdv = safe_float(first_value(item, "fdv", "marketCap", "fullyDilutedValuation", "fdvUsd"))
        liquidity = safe_float(first_value(item, "liquidity", "liquidityUsd", "liquidityUSD"))
        listing_time = first_value(item, "listingTime", "launchTime", "createTime", "createdAt")

        candidates.append({
            "market": "binance_alpha",
            "address": address,
            "symbol": symbol or alpha_id or token_id or "UNKNOWN",
            "name": name,
            "url": "https://www.binance.com/en/alpha",
            "pair_address": "",
            "created_at_ms": listing_time,
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_m5_usd": 0.0,
            # This is a best-effort token-list value; only label it 24h if supplied.
            "volume_h1_usd": volume_24h,
            "buys_m5": 0,
            "sells_m5": 0,
            "buys_h1": 0,
            "sells_h1": 0,
            "price_change_h1_pct": price_change_24h,
            "fdv_usd": fdv,
            "source": "Binance Alpha public token list",
        })

    LOG.info("Binance Alpha discovery: %d candidates; %d skipped.", len(candidates), skipped)
    if candidates:
        LOG.info(
            "Binance Alpha sample: %s (%s), identifier=%s",
            candidates[0]["name"],
            candidates[0]["symbol"],
            candidates[0]["address"],
        )
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
        if candidate["liquidity_usd"] <= 0:
            notes.append("Liquidity not supplied by the token-list feed")
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
        volume_lines = [
            f"5m volume: {fmt_usd(candidate['volume_m5_usd'])}",
            f"1h volume: {fmt_usd(candidate['volume_h1_usd'])}",
            f"5m buys/sells: {candidate['buys_m5']}/{candidate['sells_m5']}",
            f"1h price change: {candidate['price_change_h1_pct']:.2f}%",
        ]
    else:
        reasons.append("Present in the available Binance Alpha feed; verify listing and trading status")
        volume_lines = [
            f"Reported 24h volume: {fmt_usd(candidate['volume_h1_usd'])}",
            f"Reported price change: {candidate['price_change_h1_pct']:.2f}%",
        ]

    notes = risk_notes(candidate)
    lines = [
        "🔎 EARLY TOKEN RESEARCH ALERT",
        f"Market: {candidate['market'].replace('_', ' ').upper()}",
        f"Token: {candidate['name']} ({candidate['symbol']})",
        f"Address/ID: {candidate['address']}",
        f"Price: ${candidate['price_usd']:.10g}" if candidate["price_usd"] else "Price: unknown",
        f"Liquidity: {fmt_usd(candidate['liquidity_usd'])}",
        *volume_lines,
        f"FDV/market cap: {fmt_usd(candidate['fdv_usd'])}",
        "",
        "Why flagged:",
        *([f"• {reason}" for reason in reasons] or [
            "• Discovery feed match; not enough evidence to confirm momentum"
        ]),
        "",
        "Risk notes:",
        *[f"⚠️ {note}" for note in notes],
        "",
        f"Source: {candidate['source']}",
        f"Chart/source: {candidate['url']}",
    ]
    if candidate["market"] == "solana":
        lines.append(f"Solscan: https://solscan.io/token/{candidate['address']}")
    lines.extend([
        "",
        "RESEARCH ONLY — not a buy signal. Data may be incomplete; verify independently.",
    ])
    return "\n".join(lines)


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    state = load_state()
    solana_candidates = fetch_solana_candidates()
    alpha_candidates = fetch_binance_alpha_candidates()
    candidates = solana_candidates + alpha_candidates
    logging.info(
        "Fetched %d total candidates (%d Solana, %d Binance Alpha).",
        len(candidates),
        len(solana_candidates),
        len(alpha_candidates),
    )
    if not candidates:
        LOG.warning("No candidates retrieved. Check API availability and workflow logs.")

    now = int(time.time())
    cooldown = max(0, int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600")))
    max_alerts = max(0, int(os.getenv("MAX_ALERTS_PER_RUN", "5")))
    min_liquidity = max(0.0, float(os.getenv("MIN_SOLANA_LIQUIDITY_USD", "10000")))
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
            # Alert Alpha feed entries even when the token-list has sparse market fields.
            eligible = True

        should_alert = eligible and (is_new or now - last_alert >= cooldown)
        if should_alert and sent_this_run < max_alerts:
            message = make_alert(candidate, is_new, previous)
            if dry_run:
                LOG.info("DRY RUN — no Telegram message sent:\n%s", message)
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

    # Keep state bounded without deleting the duplicate-protection design.
    if len(state["snapshots"]) > 5000:
        state["snapshots"] = dict(list(state["snapshots"].items())[-4000:])
    if len(state["seen"]) > 10000:
        state["seen"] = dict(list(state["seen"].items())[-8000:])
    if len(state["last_alert"]) > 10000:
        state["last_alert"] = dict(list(state["last_alert"].items())[-8000:])
    save_state(state)
    LOG.info("Scan complete; alerts sent or simulated this run: %d", sent_this_run)


if __name__ == "__main__":
    main()
