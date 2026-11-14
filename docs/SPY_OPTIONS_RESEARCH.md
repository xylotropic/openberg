# SPY options with Drex: research and proposed paper experiment

Research date: November 14, 2026. Application baseline: `9d8cf929c21ad739ea888dd8af7a0d8a66a3f25b`.

This document proposes a new experiment. It does not describe a deployed SPY options strategy or establish profitable returns.

The user subsequently selected the stock/crypto [session strategy](SESSION_STRATEGY.md). This options proposal is retained as research and is inactive.

## Recommendation

Test a SPY price signal with a Drex context filter. Compare it with the same price signal without Drex. Evaluate actual option returns after execution costs.

Use standard SPY contracts near the money. Compare single long calls or puts with debit vertical spreads. Select a structure only when its whole-contract risk and trading costs fit the declared budget. A small premium alone does not make a contract suitable.

Keep 0DTE, naked option selling, overnight positions, and volatility-selling strategies outside the first pilot. These are scope choices for a first experiment, not findings that those strategies cannot work.

The immediate data requirement is unresolved. The current account can read indicative options snapshots, but an explicit OPRA request fails. The free feed supports development and labeled simulation. It cannot establish actual current option liquidity.

## What the evidence supports

### Drex can classify financial information. SPY option alpha still needs a test.

Nace reports finance classification benchmarks, including news, documents, and monetary-policy tasks. Its release reports weaker results for markets and reasoning than for document tasks. These vendor measurements support testing Drex as a feature extractor. They do not measure SPY option returns, executable spreads, or a prospective trading strategy. [Nace finance release](https://www.nace.ai/blog/introducing-drex-for-finance)

The Drex technical report evaluates decision accuracy and calibration on benchmark tasks. Calibration on those tasks does not transfer automatically to a particular return horizon or trading payoff. That transfer requires observations from our own task. [Drex technical report](https://arxiv.org/abs/2611.07854)

Drex returns typed answers and distributions. `noul` concerns the question asked. A `choice` distribution scores its declared alternatives. Code must read those fields directly and preserve the complete response. [Answer semantics](https://console.nace.ai/docs/drex/guides/reading-answers)

Use two distinct kinds of questions:

| Question | Meaning of its probability | Validation target |
| --- | --- | --- |
| Does this release contain a material monetary-policy change? | Confidence in a text classification | A separately labeled document judgment |
| Which SPY return interval occurs over the next 30 minutes? | An unvalidated forecast of a future market outcome | The subsequently observed SPY return |
| Does this option trade earn a positive net return? | A separate forecast that includes contract and execution details | The observed net option result |

One score cannot substitute for another. A correct interpretation of a hawkish statement does not establish a profitable put purchase.

The distinction matters economically. Research separates monetary-policy shocks from information about the economy conveyed by a central bank. Those channels can produce different stock-price responses. [Jarociński and Karadi, 2020](https://pubs.aeaweb.org/doi/10.1257/mac.20180090) A 2026 Federal Reserve survey also distinguishes several channels and communication windows. [Federal Reserve survey](https://www.federalreserve.gov/econres/feds/the-effect-of-the-federal-reserve-on-the-stock-market-magnitudes-channels-and-shocks.htm)

### Existing LLM return research does not establish this strategy

Lopez-Lira and Tang find return predictability from stock news. Their results concern individual stocks and show substantial sensitivity to turnover and costs. Effects are stronger among smaller stocks. This is a reason to test information processing, not evidence that a highly liquid index ETF option strategy inherits their returns. [Research paper](https://arxiv.org/html/2304.07619)

Gao, Han, Li, and Zhou find SPY intraday momentum in a 1993–2013 sample. The first half-hour return, measured from the previous close, predicts the last half-hour return. This provides a specific historical benchmark to replicate and challenge. It does not validate arbitrary intraday indicators or 2026 option profitability. [Published study](https://doi.org/10.1016/j.jfineco.2018.05.009)

A reproduction of that late-day rule needs its own schedule. A strategy that must exit at 15:30 Eastern cannot claim to reproduce a 15:30–16:00 return effect.

## Live integration findings

Read-only checks on November 14 found:

- The existing pinned Alpaca paper account reports options trading Level 3.
- A bounded SPY contract query returned 60 contracts across November 16–20 and strikes 750–755.
- Indicative snapshots returned HTTP 200 for all 60 contracts.
- The explicit OPRA snapshot request returned HTTP 403: `OPRA agreement is not signed`.
- The option quotes came from Friday, November 13. Saturday observations cannot establish current trading liquidity.
- The old economic-link strategy had no positions or orders. Its new entries were paused for this change.

The private probe is retained under `data/spy-options-feed-probe.json`. It contains no API credentials. No option order was submitted during this research.

Alpaca describes indicative prices as derivatives of OPRA prices. Quotes are modified, and trades are delayed derivatives. OPRA access requires the relevant entitlement. Its historical option coverage starts in February 2024. [Alpaca historical options data](https://docs.alpaca.markets/us/docs/historical-option-data)

The unsigned agreement is one observed failure. Signing it alone does not prove that the account has the required subscription. The snapshot reference describes both subscription and agreement conditions. [Snapshot reference](https://docs.alpaca.markets/us/reference/optionsnapshots)

### Cost and contract size affect the design

The following values came from the indicative probe. They illustrate contract granularity only. They are stale, modified prices, not executable opportunities.

| November 16 contract | Indicative ask | Cost of one standard contract |
| --- | ---: | ---: |
| SPY 752 call | $3.69 | $369 |
| SPY 753 call | $3.08 | $308 |
| SPY 752 put | $3.04 | $304 |

The old $250 company-exposure cap cannot fund these purchases. A new options experiment must define its own premium-at-risk limit. It must not treat one option as one share or quietly increase its risk allocation.

A narrow spread can fit a small budget while costing too much to trade. In the same indicative sample, buying the 752 call at its ask and selling the 753 call at its bid costs $67. Immediate liquidation using the opposite sides returns $48. The $19 difference equals about 28.4% of the initial debit, before fees. Different quote timestamps and the modified feed prevent a live execution conclusion. The example still shows why a low debit is insufficient.

Across this bounded 60-contract sample, the lowest outright ask cost $209 and the median cost $552.50. No outright fit a $100 premium budget. Of 120 constructed directional verticals with widths of $1–$3, 63 fit that budget. None also met a hypothetical $5 and 5%-of-debit immediate-liquidation cost screen. These are sample arithmetic results from indicative data. They do not measure SPY's actual executable liquidity or establish an optimal spread threshold.

## Strategy alternatives

These rankings are research judgments. They are not measured performance rankings.

| Candidate | Role for Drex | Main issue | Research priority |
| --- | --- | --- | --- |
| Intraday price signal with context filter | Classify trend context, relevant news, contradictions, and uncertainty | Must improve the same numeric baseline after option costs | First comparison |
| Event-driven directional options | Interpret verified macro releases and subsequent market response | Fast repricing, unavailable consensus, event-specific sample size | Separate second arm |
| Direct Drex return forecast | Predict explicit 30-minute return bins | Domain calibration and forecast contamination | Shadow benchmark |
| Long straddle or strangle around news | Classify potential volatility catalyst | Directionless movement must exceed premium and volatility repricing | Later research |
| Credit spreads or iron condors | Help identify conditions for selling volatility | Tail losses, assignment, and a different economic hypothesis | Separate later version |
| 0DTE directional options | Classify rapidly changing context | Short horizon, strong curvature, time decay, and execution sensitivity | Exclude from first pilot |

OIC describes option value as sensitive to the underlying price, time, implied volatility, dividends, and other inputs. Greeks provide estimates rather than exact premium changes. A correct SPY direction can therefore coexist with an option loss. [Options Greeks](https://www.optionseducation.org/advancedconcepts/understanding-options-greeks)

Cboe notes the sensitivity of near-the-money options close to expiration. Its published 0DTE statistics often concern SPX. SPX findings must not be labeled SPY results. [Cboe 0DTE overview](https://www.cboe.com/tradable-products/0dte)

### Outright options versus debit spreads

A long call or put has one option leg and a premium-based risk budget. It is the simpler execution benchmark. It remains eligible only if the full premium and costs fit the budget. Do not move far out of the money just to make it affordable.

A debit vertical combines two same-expiration contracts. A bullish call vertical buys the lower strike and sells the higher strike. A bearish put vertical buys the higher strike and sells the lower strike. Net debit limits the theoretical payoff loss while the short leg caps upside. Early assignment and expiration can create stock exposure beyond the intended holding. [Bull call spread](https://www.optionseducation.org/strategies/all-strategies/bull-call-spread-debit-call-spread), [bear put spread](https://www.optionseducation.org/strategies/all-strategies/bear-put-spread)

A spread is not automatically cheaper after execution costs. Both structures need a measured comparison at matched direction, horizon, and risk budget. Use a parent multi-leg limit order for a vertical. Do not open each leg independently. Alpaca documents the `mleg` order class, ratios, and explicit position intents. [Multi-leg orders](https://docs.alpaca.markets/us/docs/options-level-3-trading)

## A testable first experiment

The following parameters are proposed experimental conventions. They require registration before collecting evaluation results.

### Signal and model boundary

Start with one transparent SPY price rule. A candidate is a break above or below the first 30-minute range, with a separately specified trend confirmation. Freeze the exact calculation and its input feed. Treat this rule as an unproven baseline.

Compute prices, returns, ranges, realized volatility, and timestamps in code. Label IEX-derived volume and VWAP as venue-specific. Do not call them consolidated market volume or market-wide VWAP.

Send Drex a bounded packet containing:

- Decision time and exact forecast horizon.
- Closed SPY bars and computed features available at that time.
- The numeric candidate direction and the reason it fired.
- Recent relevant text, its original publication time, and its first observation time.
- Known scheduled events and any verified pre-release expectations.
- Missing inputs and contradictions.

Ask for regime, news relevance, economic interpretation, and conflicting evidence. Include `insufficient_evidence` in interpretive labels. A deterministic data-quality failure must block an entry before any model score is considered.

Evaluate a direct return forecast separately. One example defines five mutually exclusive 30-minute SPY return intervals: below −0.30%, −0.30% to −0.10%, −0.10% to +0.10%, +0.10% to +0.30%, and above +0.30%. Assign exact boundaries once. Preserve every probability, including predictions associated with skipped trades. These intervals are draft labels, not optimized market thresholds.

Do not ask Drex to invent contract symbols, position sizes, or order instructions. Code selects the actual contract and enforces the budget. Pin `drex-max-finance`, prompt version, and response model. Nace currently lists $0.03 per million input tokens for that model. [Model catalog](https://console.nace.ai/docs/drex/reference/models)

At that rate, 78 weekday calls with 5,000 input tokens each cost about $0.0117 per session, or $0.2457 for 21 sessions. This estimate excludes other application calls and assumes the published rate remains applicable. Preserve the existing shared request allowance and a separate monthly ceiling.

### Contract and liquidity selection

Screen standard 100-share SPY contracts with one to five exchange sessions remaining. Exclude contracts expiring on the trade date. Compare near-the-money strikes, using delta only when its source and freshness are acceptable.

Rank eligible contracts using actual two-sided quotes, quote age, displayed size, spread, and recent executable activity. Check both legs of a vertical. Store open-interest dates separately. OIC warns that open interest and volume do not guarantee liquidity. [Liquidity guidance](https://www.optionseducation.org/referencelibrary/faq/general-information)

Register quote-age, inter-leg timestamp-skew, and spread limits before the pilot. Provisional limits can be conservative, but they must be labeled parameter choices. Report how often each limit prevents a trade. Recheck quotes after the model returns and immediately before the atomic order attempt.

If complex-order quotes are unavailable, use synchronized leg bids and asks as a conservative execution bound. A multi-leg order may improve that bound. Improvement must be measured rather than assumed. Reject a trade when conservative costs erase its modeled advantage.

### Risk and schedule

| Proposed control | Initial convention |
| --- | --- |
| Virtual capital | Existing $10,000 allocation, without resetting the paper account |
| Open exposure | One SPY option group at a time |
| Daily opening-debit budget | $100 cumulative opening debits plus reserved round-trip fees; no replenishment from closing proceeds or gains |
| Entry budget | Full debit plus estimated fees must fit remaining daily capacity |
| Entries | At most two per session, without averaging down |
| Entry period | Begin with one 10:05 Eastern opportunity. Broader hours are a separate later trial |
| Main label horizon | 30 minutes |
| Secondary horizon | 60 minutes, recorded as a separate trial |
| Closing policy | Separate exit submission and verified-flat deadlines, with an explicit execution buffer |
| Holding across sessions | Excluded from the first pilot |
| Same-day expiration | Excluded from the first pilot |

Profits do not replenish the daily loss budget during the session. A percentage premium stop can be an exit trigger. It cannot guarantee the realized loss. Outages, assignment, and unsuccessful liquidation remain explicit exceptions to modeled payoff limits.

Reserve fees for opening and closing. Release the capital reservation only after a completed lifecycle; keep its opening debit consumed in the daily counter. The second entry cannot reuse that daily commitment because the first trade earned a profit. Exhausted entry capacity must never block a risk-reducing exit.

Use the actual first fill to anchor the holding clock. A provisional five-minute exit buffer separates the scheduled exit trigger from its target completion. Neither deadline guarantees a fill. If later trials target verified flatness at close minus 30 minutes, their final entry must precede that target by the holding period and exit buffer. A 60-minute trial therefore cannot accept a 14:45 entry and claim a full holding period before 15:30.

Whole-contract constraints may leave no eligible trade. Record that result. Do not expand the budget, select distant strikes, or assume midpoint fills to force activity.

SPY options are American-style ETF options. Exercise and assignment can deliver stock. They differ from cash-settled European index options. [OIC assignment guide](https://www.optionseducation.org/news/trading-options-understanding-assignment)

Reject short-call exposure around the relevant ex-dividend period. Maintain explicit early-exercise and assignment handling even with an intraday policy. Alpaca says paper non-trade activities can arrive the following day while positions change sooner. Inventory discrepancies must block new exposure until resolved. [Options account activities](https://docs.alpaca.markets/us/docs/options-trading)

### The economic decision

For an intraday option position, evaluate the liquidation value at the actual exit horizon. Do not use expiration payoff as the value of a position closed much earlier.

For a debit spread with per-share entry debit `d` and possible exit values `v[j]`:

`expected net dollars = 100 × contracts × Σ p[j] × (v[j] − d) − fees − additional execution costs`

The probabilities must concern the future outcomes used in the valuation. Semantic confidence is not a substitute. A forecast of SPY return bins also needs assumptions or observations about implied volatility and time decay before it yields option values.

Prefer subsequent observed liquidation quotes for evaluation. For modeled scenarios, use an American-option valuation method with appropriate dividend assumptions. A local Greek approximation can help explain risk. It should not supply the sole valuation for large moves or near-expiration paths.

Count spread crossing once. If entry uses ask and exit uses bid, do not deduct the full spread again. Record commissions, regulatory charges, latency, and impact assumptions separately. Show costs in dollars and as a percentage of premium.

## Testing Drex's incremental contribution

Pre-register one primary comparison: numeric signal plus Drex versus the same numeric signal without Drex. Use identical candidate times and contract rules. Also compare equal-risk exposure and turnover so a model cannot appear superior solely by making fewer trades.

Match information and execution latency in that comparison. Both arms receive the same candidate quotes and opportunity clock. Include a random-abstention control with similar participation. Compare paired daily net results on equal virtual capital before reporting per-trade improvements.

Collect all candidates, including abstentions and unfilled orders. Separate text labels, underlying return labels, executable option outcomes, and broker fills. Multiple five-minute observations from one session are correlated samples.

Use chronological training and evaluation windows. Group by session and event. Purge overlapping outcome windows and use only matured labels. Fit any probability calibration on past data only. Retain a final untouched interval and record every tested prompt and parameter variant.

An observed flat market return differs from missing data or a failed forecast. Give those states separate labels. An unavailable option outcome remains missing. Do not convert it into zero profit.

Measure Brier score, log loss, and reliability for forecast probabilities. Measure daily marked net returns, drawdown, expected shortfall, turnover, trade frequency, and fill rate for the strategy. Include open losses and inactive days. Report day-clustered uncertainty and sensitivity to worse spreads, delayed entry, and reduced fill rates.

An initial 30–60-session review can expose engineering and obvious economic failures. It does not guarantee adequate independent events or statistically reliable alpha. Model selection across many variants increases the risk of an attractive accidental backtest. [Backtest overfitting research](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)

Prospective capture is particularly important for Drex. Research shows that historical LLM forecasts can reflect memorized outcomes. Artificial cutoff instructions and masked names do not reliably solve this problem. Historical option data can test pricing and execution calculations without proving a current model's forecasting skill. [The Memorization Problem](https://arxiv.org/html/2504.14765)

Alpaca paper execution is useful for integration testing. It omits queue position, latency slippage, market impact, and other costs. The documented fill process does not enforce displayed NBBO size. Paper profits therefore need an independent, conservative execution assessment. [Paper assumptions](https://docs.alpaca.markets/us/docs/paper-trading)

## Data paths and what each can establish

| Path | Useful work | Remaining limitation |
| --- | --- | --- |
| Current Alpaca indicative feed | Contract discovery, surfaces, model packets, schema and order-lifecycle tests, labeled simulation | Modified prices cannot verify executable option liquidity or costs |
| Properly entitled Alpaca OPRA | Current consolidated BBO and prospective option execution evaluation | Agreement and entitlement are not currently verified. Paper fills remain simulations |
| Existing eligible Tradier brokerage data access | Potential separate consolidated data adapter while Alpaca handles paper execution | No such account or access is verified here. Sandbox data is delayed |
| Historical option quotes | Numeric baselines, contract selection replay, execution sensitivity | Coverage, survivorship, quote quality, and LLM hindsight still need controls |

Alpaca documents Basic indicative data and an OPRA plan. No paid upgrade was enabled. [Data plans](https://docs.alpaca.markets/us/docs/about-market-data-api)

Tradier includes API access for brokerage account holders and describes consolidated real-time stock and option data. Its sandbox uses delayed data. Greeks update hourly, and expired option history is unavailable. This is a conditional alternative, not an already connected free source. [Tradier data documentation](https://docs.tradier.com/brokerage-api/market-data), [API access and sandbox](https://docs.tradier.com/brokerage-api/getting-started)

## Required Openberg changes before autonomous option orders

1. Register a new SPY-options experiment and preserve the paused supplier experiment's record.
2. Add feed provenance, entitlement checks, quote synchronization, contract selection, and prospective candidate capture.
3. Extend the coordinator with explicit option and multi-leg authority, premium multipliers, parent/leg ownership, and SPY-underlying reservations.
4. Add option lifecycle accounting for fees, spreads, exercise, assignment, expiration, and any resulting stock obligations.
5. Add calendar-aware intraday exits that continue during model failures, pauses, and restarts.
6. Test cancellation races, late fills, missing leg reports, assignment before activity delivery, external positions, stale quotes, ambiguous parent submissions, and opening/closing net-price signs.
7. Show the new version, data mode, actual risk budget, current contracts, decisions, abstentions, and exit obligations in the UI.

The old share-based portfolio code cannot safely become an option strategy through a ticker substitution. Its sizing, holding horizon, data gates, and accounting describe another instrument.

## Activation decision

Engineering tests can run without proof of profitability. A labeled paper pilot can collect forward evidence under fixed experimental rules.

The present implementation does not yet contain that pilot. It also lacks verified OPRA access. Keep the supplier strategy paused and the proposed SPY strategy unarmed until the new instrument handling and declared data mode exist.

Do not claim that a working API, a confident model answer, or a profitable paper fill establishes an investment edge.
