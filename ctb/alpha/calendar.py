"""Alpaca exchange sessions in UTC, including holidays and early closes."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time

NY = ZoneInfo('America/New_York')


class Sessions:
    def __init__(self, client, journal):
        self.client, self.journal = client, journal
        self.rows = journal.state('exchange_calendar') or []

    @staticmethod
    def parse(row):
        def at(field):
            raw = row[field]
            if 'T' in raw:
                return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
            return datetime.fromisoformat(row['date'] + 'T' + raw).replace(tzinfo=NY).timestamp()
        return {'date': row['date'], 'open': at('open'), 'close': at('close')}

    async def refresh(self, now=None):
        now = now or time.time()
        today = datetime.fromtimestamp(now, NY).date()
        data = await self.client.request('GET', '/v2/calendar', params={
            'start': (today - timedelta(days=180)).isoformat(), 'end': (today + timedelta(days=120)).isoformat()})
        rows = sorted((self.parse(r) for r in data), key=lambda r: r['open'])
        if not rows or not any(r['open'] > now for r in rows):
            raise ValueError('The exchange calendar has no future sessions')
        self.rows = rows
        self.journal.state('exchange_calendar', rows, write=True)
        self.journal.state('calendar_retrieved_at', now, write=True)

    def current(self, now):
        return next((r for r in self.rows if r['open'] <= now < r['close']), None)

    def window(self, now):
        session = self.current(now)
        if session:
            for start in (session['open'] + 1800, session['close'] - 1800):
                if start <= now < start + 300:
                    return {'start': start, 'end': start + 300, 'session': session}
        return None

    def next_window(self, now):
        starts = [t for r in self.rows for t in (r['open'] + 1800, r['close'] - 1800) if t + 300 > now]
        return min(starts) if starts else None

    def exit_deadline(self, entry_at, sessions=10):
        entry_day = datetime.fromtimestamp(entry_at, NY).date().isoformat()
        later = [r for r in self.rows if r['date'] > entry_day]
        if len(later) < sessions:
            raise ValueError('Exchange calendar is too short for the holding deadline')
        return later[sessions - 1]['close'] - 1800

    def event_expiry(self, observed_at):
        # The first full session that starts after receipt must finish before expiration.
        future = [r for r in self.rows if r['open'] > observed_at]
        if not future:
            raise ValueError('Exchange calendar is too short for event expiration')
        return future[0]['close']

    def publication_fresh(self, earliest_publication, observed_at):
        if earliest_publication is None or earliest_publication > observed_at:
            return False
        reference = next((r for r in self.rows if r['close'] >= observed_at), None)
        if not reference:
            return False
        preceding = [r for r in self.rows if r['date'] < reference['date']]
        return bool(preceding and earliest_publication >= preceding[-1]['open'])
