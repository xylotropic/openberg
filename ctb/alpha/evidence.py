"""Immutable SEC evidence and dated, named-customer relationship candidates."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp

from ..broker.alpaca import timestamp
from .config import SEEDS, digest


class FilingText(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.parts, self.facts = [], {}
        self.numbers, self.contexts = [], {}
        self.number, self.context, self.date_field = None, None, None
        self.skipping, self.fact = 0, None
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('xbrli:context','context'):
            self.context=attrs.get('id');self.contexts[self.context]={}
        if self.context and tag.split(':')[-1] in ('startdate','enddate','instant'):
            self.date_field=tag.split(':')[-1]
        if self.context and tag.split(':')[-1] in ('explicitmember','typedmember'):
            self.contexts[self.context]['dimensional']=True
        if tag=='ix:nonfraction' and attrs.get('name') in ('us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax','us-gaap:SalesRevenueNet','us-gaap:Revenues'):
            self.number={'attributes':attrs,'text':''}
        if tag in ('script', 'style'):
            self.skipping += 1
        if tag in ('p', 'div', 'tr', 'br', 'h1', 'h2', 'h3'):
            self.parts.append('\n')
        if attrs.get('name') in ('dei:DocumentFiscalYearFocus', 'dei:DocumentFiscalPeriodFocus', 'dei:DocumentPeriodEndDate'):
            self.fact = attrs['name'].split(':')[-1]

    def handle_endtag(self, tag):
        if tag in ('xbrli:context','context'): self.context=None
        if tag.split(':')[-1] in ('startdate','enddate','instant'): self.date_field=None
        if tag=='ix:nonfraction' and self.number:
            item=self.number;attrs=item['attributes'];text=re.sub(r'[,\s$]','',item['text'])
            if re.fullmatch(r'\(?-?\d+(?:\.\d+)?\)?',text):
                value=float(text.strip('()'))*10**int(attrs.get('scale','0'))
                if attrs.get('sign')=='-' or text.startswith('('): value=-abs(value)
                if re.search(r'USD',attrs.get('unitref',''),re.I):
                    self.numbers.append({'tag':attrs['name'],'context':attrs.get('contextref'),'value':value,'unit':'USD'})
            self.number=None
        if tag in ('script', 'style'):
            self.skipping = max(0, self.skipping - 1)
        if tag in ('ix:nonnumeric', 'ix:nonfraction'):
            self.fact = None
        if tag in ('p', 'div', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.number: self.number['text']+=data
        if self.context and self.date_field:
            self.contexts[self.context][self.date_field]=(self.contexts[self.context].get(self.date_field,'')+data).strip()
        if not self.skipping:
            self.parts.append(data + ' ')
        if self.fact:
            self.facts[self.fact] = (self.facts.get(self.fact, '') + data).strip()

    @property
    def text(self):
        return '\n'.join(re.sub(r'\s+', ' ', p).strip() for p in ''.join(self.parts).splitlines() if p.strip())

    @property
    def numeric_facts(self):
        return [{**r,**self.contexts.get(r['context'],{})} for r in self.numbers]


def numeric_comparison(prior, current, scope):
    def revenue(source):
        candidates=[]
        for fact in source['metadata_json'].get('numeric_facts',[]):
            if fact.get('dimensional'): continue
            try:
                start=datetime.fromisoformat(fact['startdate']);end=datetime.fromisoformat(fact['enddate'])
            except (KeyError,ValueError): continue
            days=(end-start).days
            if (scope=='FY' and 330<=days<=400) or (scope!='FY' and 60<=days<=120):
                candidates.append(fact)
        if not candidates: return None
        last=max(f['enddate'] for f in candidates)
        candidates=[f for f in candidates if f['enddate']==last]
        values={f['value'] for f in candidates}
        return candidates[0] if len(values)==1 else None
    a,b=revenue(prior),revenue(current)
    if not a or not b or a['value']<=0: return {'status':'missing_or_ambiguous_same_scope_revenue','revenue_change':None}
    if abs((datetime.fromisoformat(b['enddate'])-datetime.fromisoformat(a['enddate'])).days-365)>15:
        return {'status':'noncomparable_periods','revenue_change':None}
    return {'status':'explicit_ixbrl_same_scope','prior':a,'current':b,'revenue_difference_usd':b['value']-a['value'],
            'revenue_change':b['value']/a['value']-1,'caveat':'Revenue change does not establish volume demand or market surprise'}


def reporting_period(facts, text, form):
    year = facts.get('DocumentFiscalYearFocus', '')
    period = facts.get('DocumentFiscalPeriodFocus', '')
    if re.fullmatch(r'20\d{2}', year) and period in ('FY', 'Q1', 'Q2', 'Q3', 'Q4'):
        return f'{year}:{"Q4" if period == "FY" else period}', period
    match = re.search(r'(?i)(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter.{0,100}?fiscal\s+(20\d{2})', text[:150000])
    reverse = re.search(r'(?i)fiscal\s+(20\d{2})\s+(first|second|third|fourth)\s+quarter', text[:150000])
    if reverse:
        return f"{reverse[1]}:{ {'first':'Q1','second':'Q2','third':'Q3','fourth':'Q4'}[reverse[2].lower()]}", {'first':'Q1','second':'Q2','third':'Q3','fourth':'Q4'}[reverse[2].lower()]
    if match:
        q = {'first': 'Q1', 'second': 'Q2', 'third': 'Q3', 'fourth': 'Q4', '1st': 'Q1', '2nd': 'Q2', '3rd': 'Q3', '4th': 'Q4'}[match[1].lower()]
        return f'{match[2]}:{q}', q
    return None, None


def customer_passages(text, name='Apple', fiscal_year=None):
    candidates = []
    paragraphs = text.splitlines()
    for paragraph_index, paragraph in enumerate(paragraphs):
        # A nearby receivables or ten-largest-customers percentage is not this customer's revenue exposure.
        for sentence_index, sentence in enumerate(re.split(r'(?<=[.!?])\s+(?=[A-Z])', paragraph)):
            if not re.search(r'\b' + re.escape(name) + r'\b', sentence, re.I):
                continue
            if not re.search(r'net (?:revenue|sales)|total (?:net )?sales', sentence, re.I):
                continue
            percentages = [float(x) for x in re.findall(r'(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)', sentence, re.I)]
            years = re.findall(r'\b20\d{2}\b', sentence)
            explicit_current = len(percentages) == 1 or bool(fiscal_year and years and years[0] == str(fiscal_year) and
                                 ('respectively' in sentence.lower() or 'during fiscal' in sentence.lower()))
            exposure = percentages[0] if percentages and explicit_current and 0 <= percentages[0] <= 100 else None
            floor = 10 if re.search(r'more than ten percent|more than 10\s*%|at least 10\s*%', sentence, re.I) else None
            if exposure is None and floor is None:
                continue
            candidates.append({'ordinal': paragraph_index * 1000 + sentence_index, 'text': sentence[:5000],
                               'exposure_pct': exposure, 'exposure_floor_pct': floor,
                               'materiality': 'explicit_named_customer_revenue'})
    return candidates


def disclosure_passages(text, limit=18000):
    """Keep source passages; no generated summary or invented financial numbers."""
    paragraphs = text.splitlines()
    selected = [p for p in paragraphs if re.search(r'demand|orders|backlog|unit sales|outlook|guidance|net sales|net revenue|pricing|inventory|customers|fiscal.*quarter', p, re.I)]
    return '\n'.join(selected)[:limit]


class Evidence:
    def __init__(self, session, research, journal, sessions, config):
        self.session, self.research, self.journal, self.sessions, self.config = session, research, journal, sessions, config
        self._sec_lock = asyncio.Lock()
        self._last_request = 0

    async def fetch(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('www.sec.gov', 'data.sec.gov'):
            raise ValueError('The v0 source collector accepts official SEC HTTPS documents')
        async with self._sec_lock:
            await asyncio.sleep(max(0, .3 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            async with self.session.get(url, headers={'User-Agent': os.environ.get('SEC_USER_AGENT', 'Openberg')},
                                        timeout=aiohttp.ClientTimeout(total=40), allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError(f'SEC source returned HTTP {response.status}')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError('SEC document exceeds the bounded v0 source size')
                return raw.decode('utf-8', errors='replace')

    async def manifest(self, symbol):
        cik = await self.research.resolve_cik(symbol)
        main = await self.research.sec(f'submissions/CIK{cik}.json')
        self.journal.insert('alpha_entities', {'symbol': symbol, 'cik': cik, 'name': main.get('name'),
                            'sector': 'XLK', 'observed_at': time.time(), 'metadata_json': {'sic': main.get('sic'), 'tickers': main.get('tickers')}})
        existing = self.journal.one('SELECT * FROM alpha_entities WHERE symbol=?', [symbol])
        if existing['cik'] != cik:
            raise ValueError('Issuer identity changed; a new verified mapping is required')
        return cik, self.research.filing_rows(main.get('filings', {}).get('recent', {}), cik)

    def store(self, *, provider, external_id, entity, raw, url, published_at, kind, metadata=None, original_start=None):
        now = time.time()
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        identity = digest([provider, external_id, content_hash])
        existing = self.journal.one('SELECT * FROM alpha_sources WHERE id=?', [identity])
        if existing:
            return existing
        previous = self.journal.one('SELECT id,observed_at FROM alpha_sources WHERE provider=? AND external_id=? ORDER BY observed_at DESC LIMIT 1', [provider, external_id])
        parsed = FilingText(raw)
        meta = {**(metadata or {}), 'document_facts': parsed.facts,'numeric_facts':parsed.numeric_facts}
        row = {'id': identity, 'provider': provider, 'external_id': external_id, 'entity': entity,
               'url': url, 'published_at': published_at, 'original_start': original_start if original_start is not None else published_at,
               'original_end': published_at, 'observed_at': now, 'retrieved_at': now, 'processed_at': now,
               'ingest_kind': kind, 'content_hash': content_hash, 'raw': raw, 'text': parsed.text,
               'metadata_json': meta, 'previous_id': previous['id'] if previous else None}
        self.journal.insert('alpha_sources', row)
        return self.journal.one('SELECT * FROM alpha_sources WHERE id=?', [identity])

    async def filing(self, symbol, row, kind):
        raw = await self.fetch(row['url'])
        documents = [{'url':row['url'],'raw':raw,'hash':hashlib.sha256(raw.encode()).hexdigest()}]
        exhibit_error = None
        if row['form'] in ('8-K', '8-K/A'):
            # Earnings release exhibits often contain the actual comparable disclosure.
            base = row['url'].rsplit('/', 1)[0] + '/'
            try:
                index = json.loads(await self.fetch(base + 'index.json'))
                names = [i['name'] for i in index.get('directory', {}).get('item', [])
                         if re.search(r'ex[-_]?99|99[._-]1', i['name'], re.I) and i['name'].lower().endswith(('.htm', '.html'))]
                for name in names[:2]:
                    if re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
                        exhibit = await self.fetch(base + name)
                        raw += '\n' + exhibit
                        documents.append({'url':base+name,'raw':exhibit,'hash':hashlib.sha256(exhibit.encode()).hexdigest()})
            except Exception as exc:
                exhibit_error = type(exc).__name__
        published = timestamp(row.get('accepted'))
        # An 8-K can be filed after the announcement. A date is an interval, not an exact release time.
        original = published or None
        if row['form'] in ('8-K','8-K/A') and row.get('report_date'):
            original = min(published, datetime.fromisoformat(row['report_date']).replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
        source = self.store(provider='sec', external_id=row['accession'], entity=symbol, raw=raw, url=row['url'],
                            published_at=published or None, original_start=original, kind=kind,
                            metadata={**row,'documents':documents,'exhibit_error':exhibit_error})
        return source

    def relationships(self, source, seed):
        year = source['metadata_json'].get('document_facts', {}).get('DocumentFiscalYearFocus')
        candidates = customer_passages(source['text'], seed['customer_name'], year)
        output = []
        for p in candidates:
            if (p['exposure_pct'] is None or p['exposure_pct'] < self.config['minimum_disclosed_exposure_pct']) and not p['exposure_floor_pct']:
                continue
            pid = digest([source['id'], p['ordinal'], p['text']])
            self.journal.insert('alpha_passages', {'id': pid, 'source_id': source['id'], 'ordinal': p['ordinal'],
                                'text': p['text'], 'content_hash': hashlib.sha256(p['text'].encode()).hexdigest()})
            identity = digest([seed['customer'], seed['supplier'], pid])
            row = {'id': identity, 'customer': seed['customer'], 'supplier': seed['supplier'], 'sector': seed['sector'],
                   'source_id': source['id'], 'passage_id': pid, 'observed_at': source['observed_at'],
                   'effective_at': source['published_at'], 'expires_at': min(source['observed_at'],source['published_at'] or source['observed_at']) + self.config['relationship_valid_days'] * 86400,
                   'exposure_pct': p['exposure_pct'], 'exposure_floor_pct': p['exposure_floor_pct'],
                   'materiality': p['materiality'], 'state': 'pending_validation', 'metadata_json': {'method': 'named-current-period-percentage', 'seed': seed}}
            self.journal.insert('alpha_relationships', row)
            output.append(identity)
        return output

    def event(self, source):
        meta = source['metadata_json']
        period, scope = reporting_period(meta.get('document_facts', {}), source['text'], meta.get('form', ''))
        identity = digest([source['entity'], period or source['external_id']])
        existing = self.journal.one('SELECT * FROM alpha_events WHERE id=?', [identity])
        self.journal.insert('alpha_event_sources', {'event_id': identity, 'source_id': source['id']})
        if existing:
            # The first observation/publication governs the event, including later copies.
            earlier = min(x for x in [existing['original_start'], source['original_start']] if x is not None) if any(x is not None for x in [existing['original_start'], source['original_start']]) else None
            values = {'original_start': earlier}
            if existing['state'] in ('pending','classified'):
                values['source_id'] = source['id']
            self.journal.update('alpha_events', identity, values)
            return self.journal.one('SELECT * FROM alpha_events WHERE id=?',[identity])
        reason = None
        if source['ingest_kind'] != 'live': reason = 'bootstrap_or_backfill'
        elif meta.get('form') not in ('8-K','8-K/A'): reason = 'original_release_not_established'
        elif meta.get('exhibit_error'): reason = 'missing_critical_exhibit'
        elif not period: reason = 'unknown_reporting_period'
        elif not self.sessions.publication_fresh(source['original_start'], source['observed_at']): reason = 'original_publication_not_fresh'
        event = {'id': identity, 'customer': source['entity'], 'period': period, 'kind': 'customer_disclosure',
                 'source_id': source['id'], 'published_at': source['published_at'], 'original_start': source['original_start'],
                 'observed_at': source['observed_at'], 'processed_at': time.time(),
                 'eligible_until': self.sessions.event_expiry(source['observed_at']), 'ingest_kind': source['ingest_kind'],
                 'state': 'abstained' if reason else 'pending', 'reason': reason,
                 'metadata_json': {'scope': scope, 'form': meta.get('form'), 'facts': meta.get('document_facts', {})}}
        self.journal.insert('alpha_events', event)
        return event

    def prior(self, event):
        if not event.get('period'):
            return None
        year, quarter = event['period'].split(':')
        desired = f'{int(year) - 1}:{quarter}'
        for source in self.journal.query('SELECT * FROM alpha_sources WHERE entity=? AND published_at<? ORDER BY published_at DESC',
                                         [event['customer'], event['published_at']]):
            period, scope = reporting_period(source['metadata_json'].get('document_facts', {}), source['text'], source['metadata_json'].get('form', ''))
            if period == desired and scope == event['metadata_json'].get('scope'):
                return source
        return None
