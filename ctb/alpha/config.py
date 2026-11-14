"""Frozen, unvalidated conventions from the user's economic-link blueprint."""
import hashlib
import json

VERSION = 'economic-links-v0.1'
PROMPT_VERSION = 'economic-evidence-v1'
MODEL = 'drex-max-finance'
SEEDS = [
    {'customer': 'AAPL', 'customer_name': 'Apple', 'supplier': 'SWKS', 'sector': 'XLK'},
    {'customer': 'AAPL', 'customer_name': 'Apple', 'supplier': 'CRUS', 'sector': 'XLK'},
]


def frozen(settings):
    return {
        'version': VERSION, 'prompt_version': PROMPT_VERSION, 'model': MODEL,
        'hypothesis': 'Sustained customer demand changes may transmit to previously documented material suppliers',
        'experimental': True, 'validated': False, 'seeds': SEEDS,
        'allocation_usd': 10000, 'company_pct': 2.5, 'event_gross_pct': 10, 'sector_gross_pct': 25,
        'gross_pct': 100, 'absolute_net_pct': 20, 'absolute_beta': .15,
        'daily_loss_pct': 1, 'drawdown_pct': 5, 'quote_age_s': 5, 'spread_bp': 15,
        'holding_sessions': 10, 'entry_window_s': 300, 'entry_ttl_s': 60, 'recovery_s': 120,
        'holding_convention': 'First exposure-producing fill is session zero; exit at close minus 30 minutes of the tenth subsequent exchange session',
        'ttl_convention': 'Request cancellation after 60 seconds or the earlier entry cutoff; broker DAY orders can still fill during a cancel/outage race',
        'recovery_convention': 'First fill plus 120 seconds; repair only before entry eligibility ends, then unwind verified owned exposure',
        'repair_after_entry_cutoff': False,
        'publication_convention': 'Earliest reliable publication or conservative date lower bound must be at/after the previous session open relative to the next/current session at observation',
        'relationship_gate': .90, 'mechanism_gate': .80, 'effect_gate': .80,
        'comparison_gate': .90, 'persistence_gate': .80, 'contradiction_max': .10,
        'materiality_gate': .90, 'minimum_disclosed_exposure_pct': 10,
        'monthly_model_usd': max(0,min(2.0, float(getattr(settings, 'alpha_monthly_model_usd', 2)))),
        'input_usd_per_token': .00000003, 'billing_verified_date': '2026-11-14',
        'borrow_annual_assumption': .03, 'financing_annual_assumption': .06,
        'extra_latency_slippage_bp': 2, 'cash_benchmark_annual_assumption': 0,
        'sortino_target_annual': 0, 'risk_lookback_sessions': 60, 'minimum_risk_sessions': 40,
        'minimum_iex_daily_dollar_volume': 1000000, 'minimum_price': 5,
        'max_estimated_round_trip_bp':50,'numeric_revenue_rule_threshold':.05,'annual_volatility_sizing_target':.15,
        'relationship_valid_days': 400, 'source_poll_s': 300, 'graph_refresh_s': 86400,
        'existing_max_positions': settings.max_positions,
        'existing_max_notional': settings.max_notional_usdt,
        'existing_max_open_risk_pct': settings.max_open_risk_pct,
        'existing_risk_per_trade_pct': settings.risk_per_trade_pct,
        'existing_max_same_side': settings.max_same_side,
        'existing_max_leverage':settings.max_leverage,
        'accounting': 'raw prices plus explicit corporate-action cash flows; gaps remain flagged',
        'source_policy': 'SEC original disclosures drive v0; news supplies context and explicit abstentions',
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
