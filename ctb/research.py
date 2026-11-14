"""Alpaca news plugin and direct SEC EDGAR all-form submissions plugin."""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

from .broker.alpaca import asset_class, timestamp


class AlpacaContext:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.on_news = []
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    async def loop(self):
        while not self._stop.is_set():
            try:
                if self.client.configured:
                    result = await self.client.request('GET', '/v1beta1/news', data_api=True,
                                                       params={'limit': 50, 'include_content': 'false', 'sort': 'desc'})
                    old = {n['id'] for n in self.store.context.get('news', [])}
                    news = []
                    for item in result.get('news', []):
                        syms = item.get('symbols') or []
                        n = {'id': str(item['id']), 'title': item['headline'], 'source': item.get('source', 'Alpaca news'),
                             'url': item.get('url'), 'published_at': item.get('created_at'),
                             'published_ts': timestamp(item.get('created_at')), 'first_seen_ts': time.time(),
                             'symbols': [s for s in self.store.symbols if s in syms or s.replace('/', '') in syms],
                             'market_wide': not syms, 'summary': item.get('summary', '')}
                        news.append(n)
                        if n['id'] not in old:
                            for callback in self.on_news:
                                await callback(n)
                    self.store.context['news'] = news
            except Exception as exc:
                self.store.record_error('newsroom:alpaca', str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


class Research:
    def __init__(self, session, alpaca):
        self.session, self.alpaca = session, alpaca
        self.cache = {}
        self.sec_lock = asyncio.Lock()
        self.sec_last = 0.0

    async def sec(self, path, *, text=False):
        if path in self.cache and time.time() - self.cache[path][0] < 300:
            return self.cache[path][1]
        # SEC requests remain below the official 10 requests/second ceiling.
        async with self.sec_lock:
            await asyncio.sleep(max(0, .25 - (time.monotonic() - self.sec_last)))
            self.sec_last = time.monotonic()
            ua = os.environ.get('SEC_USER_AGENT', 'Calibrain Harness')
            async with self.session.get('https://data.sec.gov/' + path,
                                        headers={'User-Agent': ua, 'Accept-Encoding': 'gzip, deflate'},
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    raise RuntimeError(f'SEC EDGAR returned HTTP {r.status}')
                data = await r.text() if text else await r.json()
        self.cache[path] = (time.time(), data)
        return data

    async def resolve_cik(self, query):
        query = query.strip().upper()
        if re.fullmatch(r'(CIK)?\d{1,10}', query):
            return query.removeprefix('CIK').zfill(10)
        # Official exchange/ticker mapping is hosted on www.sec.gov.
        key = '_ticker_mapping'
        if key not in self.cache or time.time() - self.cache[key][0] > 86400:
            ua = os.environ.get('SEC_USER_AGENT', 'Calibrain Harness')
            async with self.session.get('https://www.sec.gov/files/company_tickers.json', headers={'User-Agent': ua},
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    raise RuntimeError('SEC ticker lookup unavailable; enter the issuer CIK instead')
                self.cache[key] = (time.time(), await r.json(content_type=None))
        matches = [str(x['cik_str']).zfill(10) for x in self.cache[key][1].values() if x['ticker'] == query]
        if not matches:
            raise ValueError('No SEC issuer found; enter a company ticker or CIK (ETFs may file under a fund trust)')
        return matches[0]

    @staticmethod
    def filing_rows(columns, cik):
        rows = []
        for i, acc in enumerate(columns.get('accessionNumber', [])):
            def value(k):
                v = columns.get(k, [])
                return v[i] if i < len(v) else None
            root = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/'
            doc = value('primaryDocument')
            rows.append({'accession': acc, 'form': value('form'), 'filed': value('filingDate'),
                         'accepted': value('acceptanceDateTime'), 'report_date': value('reportDate'),
                         'description': value('primaryDocDescription'), 'document': doc,
                         'url': root + quote(doc, safe='') if doc else root + acc + '-index.html',
                         'filing_url': root + acc + '-index.html', 'cik': cik})
        return rows

    async def filings(self, query, *, archive=None, offset=0, limit=100, form=''):
        cik = await self.resolve_cik(query)
        main = await self.sec(f'submissions/CIK{cik}.json')
        files = main.get('filings', {}).get('files', [])
        if archive:
            if archive not in {f['name'] for f in files}:
                raise ValueError('Archive is not in this issuer\'s official submissions manifest')
            columns = await self.sec('submissions/' + archive)
        else:
            columns = main.get('filings', {}).get('recent', {})
        rows = self.filing_rows(columns, cik)
        forms = sorted({r['form'] for r in rows if r['form']})
        if form:
            rows = [r for r in rows if r['form'] == form]
        return {'plugin': 'sec-edgar', 'source': 'SEC EDGAR', 'company': main.get('name'), 'cik': cik,
                'items': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit,
                'next_offset': offset + limit if offset + limit < len(rows) else None, 'forms': forms,
                'archives': files, 'archive': archive,
                'coverage': 'Every form type in the SEC issuer manifest; older history is available in each archive below.',
                'bulk_url': 'https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip'}

    async def newsroom(self, query='', page_token=None):
        params = {'limit': 50, 'include_content': 'false', 'sort': 'desc'}
        if query:
            params['symbols'] = query.replace('/', '')
        if page_token:
            params['page_token'] = page_token
        data = await self.alpaca.request('GET', '/v1beta1/news', data_api=True, params=params)
        return {'plugin': 'alpaca-news', 'source': 'Alpaca news', 'items': data.get('news', []),
                'next_page_token': data.get('next_page_token'), 'feed': 'Alpaca news API; source attribution on each article'}

    async def options(self, underlying, *, page_token=None, expiry=None, kind=None):
        if not re.fullmatch(r'[A-Z.]{1,8}', underlying):
            raise ValueError('Enter an equity underlying symbol')
        params = {'feed': 'indicative', 'limit': 100}
        for k, v in [('page_token', page_token), ('expiration_date', expiry), ('type', kind)]:
            if v:
                params[k] = v
        data = await self.alpaca.request('GET', '/v1beta1/options/snapshots/' + underlying, data_api=True, params=params)
        return {'underlying': underlying, 'feed': 'indicative', 'items': data.get('snapshots', {}),
                'next_page_token': data.get('next_page_token'),
                'notice': 'Free indicative feed: trades are delayed and quotes are modified. Paper limit orders only.'}
