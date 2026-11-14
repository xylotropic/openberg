<p align="left"><img src="docs/brand/openberg.svg" width="64" height="64" alt="Openberg O logo"></p>

# Openberg

**An open-source, Bloomberg-inspired terminal for stocks, crypto, options, news, SEC filings, and paper trading.**

Openberg combines a dense market monitor, live order books, manual order entry, option surfaces, and model evaluation in one local application. It uses Alpaca for market data and simulated execution, with Drex Finance as the default classification model.

## Terminal

| Key | Screen | Features |
| --- | --- | --- |
| F1 | Monitor | Searchable watchlist, candles, EMA overlays, market map, live book, account, headlines, model signals |
| F2 | Evaluation | Calibration, forecast outcomes, consistency probes, model history |
| F3 | Trades | Model-owned trades, execution records, gross fill P/L |
| F4 | Newsroom | Live financial wire, ticker filters, article corrections, original links, history |
| F5 | Log | Engine, model, execution, and connection events |
| F6 | Validation | Out-of-sample checks and model skill by symbol |
| F7 | Options | Calls/puts, expiry filters, strikes, indicative bid/ask, IV, Greeks, order-ticket links |
| F8 | Filings | SEC issuer lookup, all form types, older archives, original documents and filing indexes |
| F9 | Trade | Manual paper Buy/Sell, market/limit orders, review, positions, fills, cancellation |
| F10 | Surfaces | Interactive option surfaces, point clouds, IV smiles, near-spot term structure |

Type a ticker in the command bar to select it. Commands include `ASK`, `TRADE`, `BUY`, `SELL`, `NEWS`, `OPTIONS`, `FILINGS`, `KEYS`, `STRAT`, and `UNIV`.

### Watchlist and charts

- **203 starter symbols:** 191 stocks/ETFs and 12 USD crypto pairs. The list covers broad indexes, sectors, rates, commodities, major companies, and crypto.
- Search the watchlist or use **+ ADD** to validate and save another Alpaca ticker. Up to 500 watchlist symbols are supported.
- Quote watching and the model trading universe are separate. Adding a ticker does not enable automatic trading or increase model requests.
- Alpaca Basic allows 30 stock WebSocket symbols. Openberg prioritizes selected symbols and the model universe within that limit. Other watchlist quotes refresh through batched snapshots, normally every 10 seconds.
- Historical charts load on demand. Chart intervals: **1m, 5m, 15m, 1h, 4h**. Switching instruments clears the previous plot and resets the price scale.
- Model trading profiles: **1m / 30-minute horizon**, **5m / 2 hours**, **15m / 4 hours**, **1h / 12 hours**. Chart selection does not change the trading profile.

The starter model universe is `SPY, QQQ, AAPL, NVDA, BTC/USD, ETH/USD, SOL/USD`. Its size and request budget remain explicit settings.

### Market data and source limits

| Asset/source | Data | Limits |
| --- | --- | --- |
| US stocks and ETFs | IEX trades, best bid/ask, snapshots, historical bars | One exchange. Not consolidated SIP or full-depth equity data. |
| Crypto | Alpaca US trades, quotes, live depth, historical bars | Alpaca venue depth. USD spot pairs, without crypto short opening. |
| Options | Chains, indicative quotes, IV, Greeks | Free indicative feed: modified quotes and delayed trades. Not OPRA. |
| News | Alpaca real-time news WebSocket | Actual publisher attribution on each article. Feed access follows the account entitlement. |
| Filings | SEC EDGAR public APIs | All forms in current and historical issuer manifests. Original documents and indexes are linked. |

Crypto books apply full reset snapshots, price-level updates, and zero-size deletions. A reconnect waits for a fresh snapshot. Stock ladders show IEX best bid/ask. Source timestamps, closed-market status, and connection state remain visible.

The newsroom subscribes to the full Alpaca wire. New articles reach the browser through the application WebSocket. IDs deduplicate stories, newer corrections replace old versions, and REST backfills cover reconnections. Free-account authentication and the all-news subscription were verified. No paid data upgrade is enabled by the application.

SEC access includes every form type returned by the issuer manifest, pagination, older submission archives, filing indexes/exhibits, and links to market-wide daily indexes and bulk submissions. The application retrieves records on demand.

### Option surfaces

- **3D surface or point cloud:** implied volatility, quote mid, delta, gamma, theta, or vega across strike and days to expiry.
- **IV smile:** observed volatility by strike for a selected expiry.
- **Term structure:** observed IV at the nearest available strike to spot for each expiry.
- Calls or puts, 30–365-day expiry windows, and configurable strike bands.
- Up to 6,000 chain snapshots per request, with a visible partial-chain marker when capped. Results cache for 60 seconds.
- Rotate, zoom, inspect contract values, and export charts through Plotly's controls.

Surfaces use provider values. Missing IV or Greeks remain missing, and surface gaps are not filled. Quote timestamps identify the source data. These are indicative option snapshots, not a fitted or arbitrage-clean volatility model.

### Manual paper trading

1. Choose a symbol and **Buy** or **Sell**.
2. Set quantity, order type, limit price where applicable, and time in force.
3. Select **Review Paper Order**.
4. Check the exact request and select **Send Paper Buy/Sell**.

| Asset | Orders | Time in force | Position rules |
| --- | --- | --- | --- |
| Stocks/ETFs | Market, limit | DAY | Buy, reduce long, eligible short opening, cover short. Fractional quantities depend on asset support. |
| USD crypto | Market, limit | GTC, IOC | Buy/add or sell existing inventory. Opening orders require at least $10 at the order price. |
| Standard options | Limit | DAY | Whole contracts, long calls/puts, sell to close. No opening option shorts. |

Stock and option submissions require the regular US market to be open. Crypto trades around the clock. Manual trading works while automatic trading is off.

Manual holdings have **no automatic Openberg stop or timed exit**. Options can expire or exercise into stock, with corresponding buying-power effects. The account is shared with the model, so account equity and buying power include manual activity.

Each server-side review stores an immutable request, account, quantity, position effect, and expiration. SEND atomically claims that review before making one broker submission attempt. Repeated SEND calls recover the same order ID. An ambiguous response remains unresolved until reconciled; it is never blindly resubmitted.

A durable manual ledger and symbol reservations survive restarts. The model respects manual reservations, including an option's underlying. Pending cancellation and `done_for_day` remain pending. Position changes require a new review. Closing crypto uses Alpaca's exact available balance after quantity fees. A Close/Reduce ticket cannot silently reverse a position.

Manual orders, model trades, and model forecasts have separate records. Model P/L uses model-owned fills. Account equity includes the entire paper account. Broker fees can arrive later; fill P/L is labeled gross where fees are unavailable.

### Models and strategy

**Default:** Nace.AI `drex-max-finance` through SystemOne. A Nace API key and credits are required for model requests. Market data, news, filings, and manual paper trading do not require Drex.

The inherited harness includes calibrated classification questions, mirrored consistency checks, optional model ensembles, statistical base rates, GARCH/ATR bracket sizing, cost-aware gates, confidence/volatility sizing, risk caps, drift checks, and out-of-sample validation. Records retain requests, answers, usage, latency, decisions, and later outcomes. See [configuration](ctb/config.py) and the [question profiles](ctb/kairos).

Automatic trading starts **off**. The active Alpaca adapter uses native stop-limit backstops and managed exits for model positions. Automatic option entries remain disabled with indicative data. Stock forecast windows do not cross the market close; overnight gaps are excluded from applicable base-rate windows. Resting limit entries cannot guarantee maker execution.

## Run locally

Tested with **Python 3.12** and **Node.js 22**.

```sh
git clone https://github.com/xylotropic/openberg.git
cd openberg
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
cd web-next
npm ci
npm run build
cd ..
.venv/bin/python -m ctb.server
```

Open **http://127.0.0.1:8800**. In **KEYS**, connect your own Alpaca paper API key and secret. The verified paper account ID is pinned. Add a Nace key if you want model forecasts. Set `SEC_USER_AGENT` to `Openberg your-email@example.com` for SEC requests.

You can also set `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `NACE_API_KEY`, and `SEC_USER_AGENT` in the gitignored `.env`. Alpaca is the default venue. The trading adapter's host is permanently `https://paper-api.alpaca.markets`.

### macOS background service

After building and committing a clean checkout:

```sh
.venv/bin/python scripts/service.py install
.venv/bin/python scripts/service.py status
.venv/bin/python scripts/service.py stop
```

The service installs a commit-hashed release under `~/Library/Application Support/Calibrain Harness/current`. That folder name preserves compatibility with existing installations. Live credentials, the SQLite database, and logs remain outside release directories. `release.json` records the deployed file hashes. New deployments preserve credentials and trading records.

Run one execution service per database/account. The application serves static frontend assets and its API from the same localhost origin. Node is needed to build the frontend, not to run the market-data path.

## Technical specifications

| Layer | Implementation |
| --- | --- |
| Backend | Python 3.12, asyncio, aiohttp |
| Frontend | Next.js 16.4.6, React 19.3, TypeScript, static export |
| Candles | Lightweight Charts 4.2 |
| Surfaces and research charts | Plotly.js 4.3.0, WebGL |
| Storage | SQLite WAL: forecasts, labels, model trades, manual orders, ownership reservations, runs, events |
| Browser updates | Same-origin WebSocket with changed ticks, books, bars, account and news events |
| Upstream streams | One shared connection per stock, crypto and news feed |
| Data fallback | Batched snapshots, historical-bar backfills, reconnects with bounded retry delays |
| Typography | Avenir 85 Heavy for headings/controls, Bergoom for data and chart labels |
| Palette | Black, white, orange `#ff9b00`, blue `#4da3ff`, green `#00d084`, red `#ff4d4f` |
| Branding | White O inside a black square, white Openberg wordmark |
| Secrets | Local environment only, owner-only `.env`, masked key status in the UI |
| Execution | Alpaca paper host, verified account pin, explicit manual reviews, durable order identities |
| Automation | Model requests follow an hourly budget; auto-trading is opt-in |

Avenir is a local system font and is not redistributed. Install a licensed copy to use it on other systems. Bergoom's files and [SIL OFL license](web-next/public/fonts/bergoom/LICENSE.md) are included.

## Validation

```sh
.venv/bin/python -m pytest -q tests
cd web-next && npm ci && npm run build
npm audit --omit=dev
```

Tests cover provider contracts, account ownership, order ambiguity, duplicate prevention, partial fills, quantity fees, cancellation, option ownership, source provenance, charts, watchlist separation, surfaces, and SEC history.

Live paper checks verified a crypto buy → exact-balance sell and a resting-order cancellation through the Chrome UI. Stock/option fill behavior must also be tested during an open US session. The optional `scripts/alpaca_smoke.py --execute` uses its own database and a small paper crypto round trip.

## Sources and credits

- [Alpaca market data](https://docs.alpaca.markets/us/docs/about-market-data-api), [crypto books](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data), [live news](https://docs.alpaca.markets/us/docs/streaming-real-time-news), [indicative options](https://docs.alpaca.markets/us/docs/real-time-option-data).
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
- [Bergoom](https://github.com/dchest/bergoom) by Dmitry Chestnykh, based on Source Sans 3.
- Built from [Calibrain Harness](https://github.com/zadescoxp/calibrain-harness), imported at `d9a3a62d1e64bfd28514e07ce6d300ec44654ddb`. The original MIT notice and Git history are retained. [Archived upstream documentation](docs/upstream-calibrain.md).

## License

[MIT](LICENSE). Openberg's source is open; third-party data and model access follow their providers' terms.
