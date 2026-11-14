# Economic-link paper experiment

Version: `economic-links-v0.1`. Model: `drex-max-finance`. Prompt: `economic-evidence-v1`.

This forward experiment tests whether sustained customer-demand changes affect previously documented material suppliers. It does not establish an investment edge.

## Evidence and decisions

The initial collector polls SEC submission manifests for AAPL, SWKS and CRUS every five minutes. Bootstrap includes recent supplier reports and comparable Apple reports/releases. The graph starts with named Apple exposure in supplier filings. An unnamed major customer cannot create an edge.

Sources retain original documents, hashes, revisions, URLs, publication bounds, receipt times and exact passages. SQLite triggers prevent changes to source, passage, decision, fill and cash-flow history. A later source creates another record.

The relationship must have been observed and validated before the original customer announcement. Current or historical filings discovered at startup do not qualify as fresh events. A later copy cannot reset an event's clock. Fiscal-quarter identity groups related documents.

Only original SEC 8-K earnings disclosures can trigger v0 entries. Primary reports and exhibits supply the evidence. A missing critical exhibit, unreliable publication time or missing comparable prior period causes abstention. All-form SEC browsing remains available independently in F8.

Publication dates are intervals. For an 8-K, the earlier report date supplies a conservative midnight Eastern lower bound. That lower bound must pass the original-publication freshness gate. This can reject an otherwise timely event. The collector does not claim complete issuer-news coverage.

Exact iXBRL revenue facts use dates, USD units, scaling and consolidated contexts. Code computes comparable differences when unambiguous. Revenue growth alone does not prove volume growth or a surprise against market expectations.

Drex receives untrusted source passages and bounded questions. Records contain full distributions, model IDs, prompt version, input hash, latency, usage and evidence references. Identical complete inputs use the cached classification.

| Gate | Frozen requirement |
| --- | --- |
| Named relationship | noul ≥0.90 |
| Material exposure | Explicit current revenue exposure ≥10%, or a disclosed ≥10% floor |
| Materiality classification | noul ≥0.90 |
| Comparable evidence | noul ≥0.90 |
| Sustained demand mechanism | Selected class probability ≥0.80 |
| Supplier operating effect | Positive/negative selected class probability ≥0.80 |
| Persistence | noul ≥0.80 |
| Material contradiction | noul ≤0.10 |

Price-only changes, share transfers, outages, inventory timing, financing and ambiguous mechanisms fail the mechanism gate. Classification probability is not probability of trading profit.

The model ceiling is the smaller of $2/month and `CTB_ALPHA_MONTHLY_MODEL_USD` at version registration. Requests also consume the intraday model's existing hourly allowance. The journal reserves the maximum Finance request cost before dispatch. Failed or uncertain billing retains that reservation. There are no automatic top-ups.

## Portfolio and account

The strategy shares the configured paper account. It never creates an account, resets balances or repoints existing credentials. Activation reserves the smaller of $10,000 and explicit unencumbered positive cash/equity capital. Margin buying power does not define the virtual allocation.

Targets precede orders. Stock and sector-ETF hedge quantities use whole shares and up to 60 aligned closed-session adjusted returns. At least 40 returns are required. Missing sessions are not zeros. Volatility sizing uses a frozen 15% annual target before applying exposure caps. Hedge ratios use stock/ETF betas to SPY.

The strategy can reject a valid signal when whole shares do not fit. An ETF hedge has exclusive ownership. A second group cannot casually share it.

| Limit | Default |
| --- | --- |
| Company | 2.5% of allocation |
| Event cluster gross, including hedge | 10% |
| Sector gross | 25% |
| Strategy gross | 100% |
| Absolute net | 20% |
| Estimated absolute market beta | 0.15 of strategy NAV |
| Entry quote age | ≤5 seconds |
| Displayed IEX spread | ≤15 bp |
| IEX daily dollar volume | ≥$1 million |
| Minimum stock price | $5 |
| Estimated round-trip friction | ≤50 bp |
| Daily marked loss | 1% pauses entries |
| Estimated-net drawdown | 5% requires a new versioned review |

Tighter existing limits remain binding. Full strategy notional supplies a conservative whole-account risk charge. Manual holdings and pending orders consume real capacity without entering this strategy's returns.

IEX is venue data, not consolidated NBBO. Current tradability, shortability and easy-to-borrow eligibility are required. Actual borrow availability or future borrowing rates are not guaranteed.

## Execution and exits

Every production writer uses the same coordinator. Its OS file lock survives release changes and prevents another cooperating Openberg process from writing the pinned account. Canonical intents identify the exact manual review, legacy trade or experiment group leg. Prefixes and symbols alone do not establish ownership.

The coordinator atomically claims an attempt before POST. The same transaction updates the originating attempt marker. A timeout or ambiguous 404 never authorizes another POST or a new ID. Resource leases and pending capacity remain reserved through uncertain outcomes.

Entry windows start at session open plus 30 minutes and session close minus 30 minutes. Each lasts five minutes. Events expire after the first complete session starting after receipt. Publication freshness is a separate condition.

Entries use regular-hours DAY instructions. The local service requests cancellation after 60 seconds or the earlier eligibility cutoff. This is a cancellation policy, not a broker-enforced 60-second expiry. Late fills remain owned.

The first actual exposure-producing fill anchors session zero and a two-minute recovery clock. Existing entry legs can complete within their earlier eligibility deadline. An incomplete group cancels remaining entries and unwinds only reconciled owned quantity. It does not chase another hedge after eligibility ends.

The scheduled exit is 30 minutes before close on the tenth subsequent exchange session. This explicit convention uses the broker calendar, including holidays, daylight-saving changes and early closes.

Execution and exit workers do not depend on Drex, source ingestion or shadow research. A pause blocks new exposure. Existing obligations remain active. Broker outages, halts and closed sessions can leave an overdue exit. The service never promises flatness within two minutes.

Unexplained inventory or activity causes quarantine. An exit cannot reverse a position or consume another owner's quantity. No account-wide cancel-all or close-all endpoint is used.

## Accounting and research

The journal separates broker-booked fills, strategy ownership and estimated economic results. It does not use the account's total equity curve as strategy performance.

Raw IEX prices mark the owned book. The economic view adds dividend receivables or short liabilities and modeled costs. Actual referenced cash events replace accruals. Late fee corrections retain their original execution or entitlement link. Unresolved attribution remains a gap.

Borrow costs assume 3% annually. Financing assumes 6% on negative strategy cash. Extra latency/slippage assumes 2 bp per fill. Fill prices already include spread crossing, so the economic ledger does not deduct the spread again. Paper execution omits real-market frictions.

Corporate actions are polled from Alpaca with pagination and retained versions. Provider delivery can lag. Known noncash actions in the proposed holding period reject a candidate. An unexpected split or other unsupported noncash event creates a visible accounting gap and blocks new entries. Economic split share effects are separate from broker quantities. Automatic quantity adoption requires verified ownership; the service does not guess through an unexplained change.

Daily marked NAV includes inactive exchange sessions and unrealized losses. Borrowing accrues through weekends. Recent daily marks can be revised from later evidence while preserving earlier versions.

| Measure | Definition |
| --- | --- |
| Cash benchmark / Sortino target | Assumed 0% annually |
| Sharpe | Mean daily net excess return divided by sample standard deviation |
| Sortino | Mean daily target excess return divided by the root mean squared downside across **all** periods |
| Annualization | Descriptive 252-session convention |
| Sharpe uncertainty | Seeded 10-session moving-block bootstrap interval |
| Alpha | OLS intercept against SPY and XLK after 60 aligned sessions, with Newey-West lag-10 uncertainty |
| Expected shortfall | Mean of the worst 5% of daily net returns |
| Drawdown | Decline from the marked estimated-net NAV peak |
| Turnover | Absolute owned fill notional, reported separately from returns |

Ratios require at least 20 sessions, usable variance and at least three downside observations. Sparse or unavailable data remains insufficient. Event returns do not supply portfolio Sharpe.

Registered shadow variants include numeric/keyword rules, lagged price-sector reaction, graph-only, industry-preserving shuffled graphs, delayed entry, higher costs, long-only and unhedged stocks. A degenerate shuffle with one customer remains unavailable. Shadow sampling uses observed IEX bars and explicit costs. It is not an executable fill simulation.

Labels mature after ten sessions. Chronological splits keep event clusters together and purge overlapping label periods. Weekly training waits for at least 30 independent matured events. Its small ridge model remains in shadow. It cannot change the active model, prompt or parameters.

The starter graph is narrow and lacks a survivorship-complete historical universe. Pretrained model knowledge is not point-in-time historical training data. A running service and successful paper fills do not demonstrate alpha.

## Controls and deployment

Open **F11 / ALPHA**, or type `ALPHA` in the command bar.

- **ARM PAPER / RESUME ENTRIES:** verify readiness, then enable this experiment's persisted entry flag.
- **PAUSE ENTRIES:** preserve exit and recovery obligations.
- **CANCEL OWNED ENTRIES:** cancel this experiment's entries and unwind incomplete groups.
- **CLOSE STRATEGY BOOK:** pause entries and close only this experiment's reconciled holdings.

The intraday AUTO-TRADE switch is separate. Manual holdings never receive this experiment's time exits.

The Mac must stay awake and online. Heartbeats expose missed schedules and failures. Launchd restarts the local service, but it cannot trade through sleep or lost connectivity.

The release installer stops the old process, backs up SQLite, preserves the runtime `.env` and database, then switches the release symlink. Manifest hashes identify served files.

**Rollback:** first pause this experiment through `/api/alpha/control` with `{"action":"pause"}`. Keep the coordinator and exit workers running while any group/order remains unresolved. A frontend rollback can preserve this backend. Do not start a pre-coordinator backend against active obligations. Reverting the full backend requires verified flat strategy inventory, terminal owned orders and reconciled manual/legacy attempts. Do not restore an old database over newer activity.

Useful local endpoints: `GET /api/alpha`, `POST /api/alpha/control`, `GET /api/alpha/evidence/{id}`, and `GET /api/alpha/classification/{id}`. Mutations require the existing same-origin policy. Credentials remain local and outside release/Git files.

## References

- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Nace models](https://console.nace.ai/docs/drex/reference/models), [answer semantics](https://console.nace.ai/docs/drex/guides/reading-answers), [pricing](https://console.nace.ai/docs/drex/reference/pricing)
- [Alpaca paper assumptions](https://docs.alpaca.markets/us/docs/paper-trading), [orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1)
