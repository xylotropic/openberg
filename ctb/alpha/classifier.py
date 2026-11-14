"""Bounded Drex evidence judgments with a durable, conservative spend ceiling."""
import math
import time
from datetime import datetime, timezone

import aiohttp

from ..kairos.client import SystemOneClient
from ..models import MODELS
from .config import MODEL, PROMPT_VERSION, VERSION, digest


def noul(answers, key):
    answer = answers.get(key, {})
    value = answer.get('noul')
    if answer.get('type') != 'noul' or type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Missing or invalid noul answer: ' + key)
    return float(value)


def choice(answers, key, allowed):
    answer = answers.get(key, {})
    distribution = answer.get('probabilities')
    label = answer.get('choice')
    if answer.get('type') != 'choice' or label not in allowed or not isinstance(distribution, dict) or set(distribution) != set(allowed):
        raise ValueError('Missing or invalid choice distribution: ' + key)
    if not all(type(p) in (float, int) and math.isfinite(p) and 0 <= p <= 1 for p in distribution.values()) or abs(sum(distribution.values()) - 1) > .02:
        raise ValueError('Malformed choice probabilities: ' + key)
    return label, float(distribution[label])


MECHANISMS = {
    'sustained_demand_growth': 'Persistent growth in underlying customer demand, beyond a one-off movement',
    'sustained_demand_decline': 'Persistent deterioration in underlying customer demand',
    'price_only': 'Revenue changes primarily because prices change, without a supported volume-demand change',
    'share_transfer': 'Demand shifts between competitors rather than changing at the economic level',
    'temporary_disruption': 'Temporary production, outage, timing, or supply disruption',
    'inventory': 'Inventory timing or channel movement without sustained end demand evidence',
    'other': 'Financing, acquisition, accounting, or another unsupported mechanism',
    'insufficient_evidence': 'The evidence does not support a clear mechanism',
}
EFFECTS = {'positive': 'Supported beneficial operating effect on this supplier', 'negative': 'Supported adverse operating effect',
           'mixed': 'Material offsetting effects', 'none': 'No supported material effect',
           'insufficient_evidence': 'Exposure or transmission is not established'}


def relationship_questions():
    return {
        'relationship_supported': {'type': 'noul', 'instructions': 'Does E1 explicitly identify the stated customer and supplier relationship? An unnamed major customer is insufficient.'},
        'materiality': {'type': 'noul', 'instructions': 'Does E1 explicitly establish at least ten percent of the supplier current-period revenue or net sales from the named customer? Do not infer or invent a percentage.'},
    }


def event_questions():
    return {
        **relationship_questions(),
        'mechanism': {'type': 'choice', 'instructions': 'Which economic mechanism explains the supported change from E2 to E3? Revenue growth alone is not proof of volume demand.', 'criteria': MECHANISMS},
        'target_effect': {'type': 'choice', 'instructions': 'What effect on the target supplier operating business is supported by E1–E4? Judge the economic effect, not the future stock return.', 'criteria': EFFECTS},
        'comparison_valid': {'type': 'noul', 'instructions': 'Are E2 and E3 comparable in fiscal period, currency, units, accounting basis and business scope for the claimed change? Missing critical comparison means no.'},
        'persistence': {'type': 'noul', 'instructions': 'Does the evidence support sustained underlying demand rather than a temporary or conditional change?'},
        'material_contradiction': {'type': 'noul', 'instructions': 'Does any supplied evidence materially contradict or qualify the proposed clear demand-transmission effect?'},
    }


class Classifier:
    def __init__(self, session, journal, config, shared_budget, *, client=None):
        self.session, self.journal, self.config, self.shared_budget = session, journal, config, shared_budget
        self.client = client or SystemOneClient(MODELS['drex'], session, model=MODEL, timeout_s=60, max_retries=0)

    async def availability(self):
        if not self.client.configured:
            raise ValueError('Nace API key is not configured')
        spec = MODELS['drex']
        if spec.base_url != 'https://console.nace.ai':
            raise ValueError('The experiment requires the verified Nace endpoint')
        async with self.session.get(spec.base_url + '/v1/models', headers={'Authorization': 'Bearer ' + spec.api_key},
                                    timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f'Drex model availability returned HTTP {response.status}')
            body = await response.json()
        models = body.get('models', body.get('data', [])) if isinstance(body, dict) else body
        names = [x.get('name') or x.get('id') for x in models]
        if MODEL not in names:
            raise ValueError('drex-max-finance is unavailable to the configured API key')
        self.journal.state('model_availability', {'model': MODEL, 'verified_at': time.time(), 'available': True}, write=True)

    def spend(self, now=None):
        now = now or time.time()
        month = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self.journal.one('SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) amount FROM alpha_classifications WHERE started_at>=?', [month])
        return float(row['amount'])

    async def ask(self, purpose, state, questions, evidence):
        payload = {'model': MODEL, 'state': state, 'questions': questions}
        identity = digest([MODEL, PROMPT_VERSION, payload])
        cached = self.journal.one('SELECT * FROM alpha_classifications WHERE id=?', [identity])
        if cached:
            if cached['state'] != 'complete':
                raise ValueError('The exact classification is unresolved or failed: ' + str(cached.get('error') or cached['state']))
            return cached
        # Every successful Finance request is bounded by 262,144 input tokens.
        reserve = 262144 * self.config['input_usd_per_token']
        now = time.time()
        month = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.journal.db.lock:
            self.journal.db.conn.execute('BEGIN IMMEDIATE')
            try:
                spent = self.journal.db.conn.execute('SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) FROM alpha_classifications WHERE started_at>=?', [month]).fetchone()[0]
                if spent + reserve > self.config['monthly_model_usd']:
                    raise ValueError('The frozen monthly experiment model budget is exhausted')
                if not self.shared_budget.try_take():
                    raise ValueError('The existing shared hourly model budget is exhausted')
                row = self.journal.encode({'id': identity, 'version': VERSION, 'purpose': purpose, 'requested_model': MODEL,
                    'prompt_version': PROMPT_VERSION, 'input_hash': digest(payload), 'request_json': payload,
                    'evidence_json': evidence, 'started_at': now, 'state': 'reserved', 'reserved_usd': reserve})
                self.journal.db.conn.execute(f"INSERT INTO alpha_classifications ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
                self.journal.db.conn.execute('COMMIT')
            except BaseException:
                self.journal.db.conn.execute('ROLLBACK')
                raise
        try:
            result = await self.client.ask(state, questions)
            if result.metadata.get('model') != MODEL:
                raise ValueError('The responding model differs from drex-max-finance')
            for key, q in questions.items():
                if q['type'] == 'noul': noul(result.answers, key)
                elif q['type'] == 'choice': choice(result.answers, key, q['criteria'])
            tokens = result.usage.get('input_tokens')
            cost = tokens * self.config['input_usd_per_token'] if isinstance(tokens, int) and 0 <= tokens <= 262144 else None
            self.journal.update('alpha_classifications', identity, {'responding_model': result.metadata['model'],
                'response_json': result.raw, 'usage_json': result.usage, 'latency_ms': result.latency_ms,
                'actual_usd': cost, 'completed_at': time.time(), 'state': 'complete'})
        except Exception as exc:
            message=str(exc)
            secret=MODELS['drex'].api_key
            if secret: message=message.replace(secret,'[redacted]')
            self.journal.update('alpha_classifications', identity, {'state': 'failed', 'completed_at': time.time(), 'error': message[:500]})
            raise ValueError(message[:500]) from None
        return self.journal.one('SELECT * FROM alpha_classifications WHERE id=?', [identity])


def gates(answers, config):
    mechanism, mechanism_p = choice(answers, 'mechanism', MECHANISMS)
    effect, effect_p = choice(answers, 'target_effect', EFFECTS)
    checks = [
        (noul(answers, 'relationship_supported') >= config['relationship_gate'], 'relationship_support'),
        (noul(answers, 'materiality') >= config['materiality_gate'], 'materiality'),
        (noul(answers, 'comparison_valid') >= config['comparison_gate'], 'noncomparable_evidence'),
        (mechanism in ('sustained_demand_growth', 'sustained_demand_decline') and mechanism_p >= config['mechanism_gate'], 'unsupported_or_uncertain_mechanism'),
        (effect in ('positive', 'negative') and effect_p >= config['effect_gate'], 'uncertain_target_effect'),
        (noul(answers, 'persistence') >= config['persistence_gate'], 'insufficient_persistence'),
        (noul(answers, 'material_contradiction') <= config['contradiction_max'], 'material_contradiction'),
    ]
    failed = [reason for ok, reason in checks if not ok]
    return {'action': 'long' if effect == 'positive' else 'short' if effect == 'negative' else None,
            'eligible': not failed, 'reasons': failed, 'mechanism': mechanism, 'mechanism_probability': mechanism_p,
            'effect': effect, 'effect_probability': effect_p}
