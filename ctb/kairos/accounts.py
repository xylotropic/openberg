"""Private prepaid credential pool. Account aliases, never keys, enter the journal."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from .client import SystemOneClient
from ..models import MODELS


class AccountPool:
    def __init__(self, db, session, model):
        self.db=db;self.accounts=[];self.clients={}
        path=os.environ.get('NACE_ACCOUNTS_FILE','').strip()
        if path:
            file=Path(path).expanduser()
            info=file.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
                raise ValueError('Drex account file must be owned by this user with mode 0600')
            rows=json.loads(file.read_text()).get('accounts',[])
            if not rows or len(rows)>32: raise ValueError('Drex pool requires 1–32 accounts')
            seen=set();ids=set()
            for row in rows:
                identity=row.get('id','');key=row.get('api_key','')
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',identity) or identity in ids:
                    raise ValueError('Drex account aliases must be unique simple identifiers')
                if not isinstance(key,str) or not key.startswith('nace_sk_') or len(key)<24 or row.get('prepaid_only') is not True:
                    raise ValueError('Drex pool needs complete keys and verified prepaid-only accounts')
                fingerprint=hashlib.sha256(key.encode()).hexdigest()
                if fingerprint in seen: raise ValueError('Duplicate Drex credential in account pool')
                seen.add(fingerprint);ids.add(identity)
                self.accounts.append({'id':identity,'key':key})
                self.clients[identity]=SystemOneClient(MODELS['drex'],session,model=model,timeout_s=25,max_retries=0,api_key_getter=lambda key=key:key)

    @property
    def configured(self): return bool(self.accounts)

    def state(self,key,value=None,write=False):
        key='credential:'+key
        if write:
            with self.db.lock:
                self.db.conn.execute('INSERT INTO scalper_state VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json',[key,json.dumps(value)])
        row=self.db.one('SELECT value_json FROM scalper_state WHERE id=?',[key])
        return json.loads(row['value_json']) if row else None

    def choose(self,now=None):
        now=now or time.time()
        if not self.accounts: return None
        cursor=self.state('cursor') or 0
        for offset in range(len(self.accounts)):
            index=(cursor+offset)%len(self.accounts);identity=self.accounts[index]['id']
            if not self.available(identity,now): continue
            self.state('cursor',(index+1)%len(self.accounts),write=True)
            return self.clients[identity],identity
        return None

    def available(self,identity,now=None):
        state=self.state('account:'+identity) or {}
        return not state.get('blocked') and state.get('retry_at',0)<=(now or time.time())

    def failed(self,identity,exc,now=None):
        now=now or time.time();code=getattr(exc,'status',None)
        if code in (401,402,403):
            self.state('account:'+identity,{'blocked':True,'at':now,'reason':'Credit unavailable' if code==402 else 'Credential unavailable','http_status':code},write=True)
        elif code==429:
            self.state('account:'+identity,{'retry_at':now+300,'reason':'Rate limited','http_status':code},write=True)

    def redact(self,message):
        for key in [MODELS['drex'].api_key,*(a['key'] for a in self.accounts)]:
            if key: message=message.replace(key,'[redacted]')
        return re.sub(r'nace_sk_[A-Za-z0-9_-]+','[redacted]',message)

    def status(self):
        rows=[]
        for account in self.accounts:
            identity=account['id'];state=self.state('account:'+identity) or {}
            usage=self.db.one('SELECT COUNT(*) calls,COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) spent_usd FROM scalper_calls WHERE credential_id=?',[identity])
            rows.append({'id':identity,'state':'BLOCKED' if state.get('blocked') else 'COOLDOWN' if state.get('retry_at',0)>time.time() else 'READY',**state,**usage})
        return rows
