# Session strategy

Openberg scans every 30 seconds. The Alpaca clock selects the entry universe:

- US regular session: SPY every 30 seconds; TSLA, AAPL, GOOGL, MSFT, NVDA, AMD and INTC shares every 10 minutes.
- Closed session: BTC/USD, ETH/USD and SOL/USD spot every 30 seconds.

Stock targets are LONG, SHORT, HOLD and EXIT. Crypto targets are LONG, HOLD and EXIT because Alpaca spot does not support opening shorts. Shorts require current shortable and easy-to-borrow flags. The broker clock handles holidays and early closes. New entries stop 35 minutes before the next session boundary. The service closes its positions from the previous universe and waits for reconciliation before opening positions in the new universe. Manual and external positions retain their ownership.

This is an unvalidated paper experiment. It has no demonstrated return advantage. It does not trade options.

## Model packet

Each eligible symbol supplies 30 closed one-minute, 12 closed five-minute and eight closed fifteen-minute candles. All windows derive from the same minute history. The service excludes forming and incomplete buckets and rejects gaps in the required windows. It retains up to 500 consecutive minute bars for indicator warmup; it needs at least 130 before making a packet. Depending on the fifteen-minute boundary, a complete window can require more.

One-minute features are EMA 9/21, Wilder RSI 7/14, Wilder ATR 14, rolling 60-minute volume-weighted price, Bollinger %B and width (20, two population standard deviations), MACD histogram (12/26/9), last-minute volume divided by the prior 20-minute mean, returns over 1/5/15/60 elapsed minutes, 60-minute realized volatility (sample standard deviation of log returns, percent per minute), and position in the last hour's range. VWAP uses provider bar VWAP multiplied by volume. IEX metrics cover that venue, not the consolidated US market.

A packet also includes the executable quote, spread, observed timestamp, strategy-owned position and a 30-minute horizon. Quotes must be no older than five seconds. Stock spreads must be at most five basis points; crypto spreads at most 20. Missing or stale data blocks that symbol.

## Budget and cadence

The application reserves the maximum possible Finance request charge before each send: 262,144 input tokens at $0.03 per million. Reservations and usage survive restarts in SQLite. Valid reported input usage releases the unused reserve; failed or ambiguous requests keep their reservation. Requests have no automatic retries. The monthly ledger includes the prior economic-link experiment's expenditure and the separately labeled engineering probe. It does not represent all spending by other applications on the same Nace account.

The former $2 monthly ceiling is removed for this strategy. The shared hourly request limit remains. Each symbol's last reservation determines its next due time, including across restarts. Due symbols share one batch; missed intervals do not cause catch-up bursts. Data readiness, paused entries, session transitions and request failures can skip an interval. Broker execution runs separately so a pending maker order does not delay the next model request. Queued entries still expire after 25 seconds and cannot pyramid.

Set `NACE_ACCOUNTS_FILE` to an owner-only JSON file outside the repository. Its `accounts` array contains unique aliases (`id`), full `api_key` values and `prepaid_only: true`, after verifying that paid usage is off in each account's billing settings. Openberg rotates batches across these accounts. It stores only the alias in each call record; keys never enter SQLite, API responses or the frontend. Without this file, it uses the existing `NACE_API_KEY`.

HTTP 402 blocks an account persistently across restarts; 401/403 also disable the credential. A 429 pauses that credential for five minutes. There is no immediate retry with a different account. Calls stop when no eligible credential remains. Openberg does not enable paid usage, top up balances, or guarantee that a previously verified provider billing setting cannot change externally. Risk-reducing orders do not depend on Drex availability. The local usage ledger does not measure spending by other applications or credit expiration.

## Orders and risk

LONG and SHORT are target directions. Repeated signals never pyramid. HOLD preserves the current strategy position or remains flat. EXIT closes only proven strategy-owned inventory. An opposite signal closes first; a later decision may reopen after reconciliation and a five-minute cooldown.

Each new position targets `CTB_SCALPER_ALLOCATION_PCT` percent of current paper-account equity, rounded down to the permitted asset quantity. The default is 5; set it to 20 for a 20% allocation. This is capital allocation, not the portfolio loss at the stop. Fresh buying power and existing aggregate risk limits can reduce the size. The coordinator rechecks this allocation against current account equity immediately before dispatch. Changing the allocation starts a new run and leaves previous trades under their original run. Stops use the greater of 1.5 times one-minute ATR or 20 basis points for stocks and 100 basis points for crypto. Targets are twice the stop distance. The time exit is 30 minutes. Existing account limits, kill controls, leverage limits and exclusive resource leases still apply. Stops and time exits are checked by the existing five-second execution worker, with broker stop-limit backstops. These are triggers and sizing limits, not guaranteed loss ceilings or guaranteed fills.

The account loss guard saves an immutable account-specific reference from the first valid prior-close equity, or from the first positive equity observation when the account has no prior close. It compares current equity against that fixed reference and any valid broker prior close, using the configured loss percentage against each reference. Either breach blocks automatic entries persistently, including after equity recovers or the service restarts. F12 shows the reference, capture time and entry floor. A missing or unwritable guard blocks entries without disabling exits, reconciliation or manual trading. A storage failure also writes an account-specific entry-block marker next to the kill file. There is no automatic latch reset.

These are whole-account equity-change checks. Deposits, withdrawals, fees and manual/external activity affect them. They are not funding-adjusted strategy returns or guaranteed loss limits. A first-observation reference does not cover losses before that observation. Unknown prior-day P&L remains unknown.

The new version uses exact version-linked decision records and the existing paper executor/coordinator. It preserves durable client order IDs, ownership reconciliation, unknown-response recovery and cross-owner reservations. It rechecks signal age, candle cutoff, quote age, session and allocation caps before dispatch. A terminal preflight rejection can release an entry only when the canonical attempt marker is null. Lookup failures after an attempted submission retain ownership and never authorize another POST. The old per-symbol model path and economic-link entries/classifications are disabled while this version is active; their records and exit obligations remain.

The 0.60 action-weight gate is a provisional execution filter. A choice weight is not a calibrated probability of profit. Crypto cost estimates include 50 basis points of round-trip fee allowance plus the observed spread; stock shadow outcomes use the observed spread plus two basis points. Actual fills and fees can differ. The equity curve uses account-scoped snapshots recorded from successful broker syncs while Openberg runs, with one observation per minute and coarser grouping for longer views. History survives restarts and begins when recording starts. The curve includes fees, manual activity and cash movements. It is not a strategy-only return curve and does not backfill from an inconsistent provider history series.

## Controls

Open **F12 STRATEGY** to inspect session selection, readiness, budget, calls and decisions. Pause/resume changes the entry switch and persists it. The kill switch disables entries and requests closure of owned bot positions. Exit workers continue while entries or Drex calls are paused. The chart FULLSCREEN control supports exit through its button or Escape. The monitor shows Alpaca paper-account equity in place of relative symbol performance.

Unrealized P&L uses fresh bid prices for longs and ask prices for shorts, before fees. Quotes push position updates over the live socket; stale or unavailable quotes show broker marks. Equity and cash remain broker-reported. Charts label broker-confirmed executions **LONG FILL**, **SHORT FILL** and **EXIT FILL**, separately from strategy signals.

## Backout

Before rolling back to a release without the persistent account guard, set `CTB_TRADING_ENABLED=0` in the live environment and stop the service. Switch only the release pointer, then restart with the latest database and credentials. Keep the guard table and any entry-block markers. Verify entries are disabled and existing exit/reconciliation workers are running. Restoring an older database would lose order ownership and is not a supported rollback.

## Sources

- [Drex models and pricing](https://console.nace.ai/docs/drex/reference/models)
- [Alpaca stock and options data coverage](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Alpaca crypto trading and fees](https://docs.alpaca.markets/us/docs/crypto-trading)
- [Alpaca paper simulation limitations](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca account portfolio history](https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1)
