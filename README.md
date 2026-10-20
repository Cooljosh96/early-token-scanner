# Early Token Scanner

Research-only Telegram scanner for Solana token discovery and best-effort Binance Alpha feed monitoring.

## Features
- Solana token profile discovery and reported pair activity through DEX Screener.
- Best-effort Binance Alpha token-feed integration. Binance can change endpoint schemas; verify that this feed is returning data in GitHub Actions logs.
- Basic liquidity and activity screening.
- Telegram alerts with chart/explorer links and explicit risk warnings.
- Persistent state file to reduce duplicate alerts.
- No trading, wallet connections, or automatic purchases.

## GitHub repository setup
1. Add repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. Ensure GitHub Actions is enabled.
3. Commit the files while preserving the `.github/workflows/scanner.yml` path.
4. Open **Actions → Early Token Scanner → Run workflow**.
5. Start with `dry_run: true`. This logs candidate alerts but does not send Telegram messages.
6. Review the run logs. Then run manually with `dry_run: false` to test Telegram delivery.
7. The scheduled workflow runs every five minutes, subject to GitHub Actions scheduling and usage limits.

## Persistent state
The workflow commits `data/early-token-scanner-state.json` back to this repository. The workflow therefore needs repository contents write permission. Do not store secrets in the state file.

## Important limitations
- Discovery feeds are not a complete census of new tokens.
- The Binance Alpha endpoint is best-effort and may need maintenance if Binance changes it.
- This is not a contract audit. Solana mint/freeze authority, holder concentration, LP lock/burn status, sellability, and other risks need independent checks.
- Missing data means unknown, not safe.
- Volume and transaction counts can be manipulated. A new pool or alert is not a buy recommendation.
- Never provide wallet seed phrases or private keys to this project.
