import json
import pytest

from ctb.db import DB
from ctb.kairos.accounts import AccountPool
from ctb.kairos.client import KairosError,RateBudget
from ctb.scalper import Spend,MODEL


def pool(tmp_path,monkeypatch,rows=None):
    path=tmp_path/'private.json'
    path.write_text(json.dumps({'accounts':rows or [{'id':f'drex-{n}','api_key':f'nace_sk_synthetic_credential_{n}','prepaid_only':True} for n in range(3)]}))
    path.chmod(0o600);monkeypatch.setenv('NACE_ACCOUNTS_FILE',str(path))
    db=DB(tmp_path/'test.db');Spend(db,RateBudget(200))
    return AccountPool(db,None,MODEL),path


def test_round_robin_usage_attribution_and_durable_credit_stop(tmp_path,monkeypatch):
    p,path=pool(tmp_path,monkeypatch)
    assert [p.choose()[1] for _ in range(4)]==['drex-0','drex-1','drex-2','drex-0']
    p.failed('drex-1',KairosError('billing','no credits',402))
    reopened=AccountPool(p.db,None,MODEL)
    assert reopened.choose()[1]=='drex-2'
    p.failed('drex-0',KairosError('auth','invalid',401));p.failed('drex-2',KairosError('billing','no credit',402))
    assert p.choose() is None
    text=json.dumps(p.status())
    assert 'nace_sk_' not in text and 'BLOCKED' in text
    assert p.redact('failed nace_sk_synthetic_credential_1')=='failed [redacted]'


def test_rate_limit_waits_and_preserves_restart_cursor(tmp_path,monkeypatch):
    p,path=pool(tmp_path,monkeypatch)
    assert p.choose(now=1000)[1]=='drex-0'
    p.failed('drex-1',KairosError('rate','wait',429),now=1000)
    assert AccountPool(p.db,None,MODEL).choose(now=1001)[1]=='drex-2'
    assert p.choose(now=1301)[1]=='drex-0' and p.choose(now=1301)[1]=='drex-1'


def test_rejects_readable_secret_file_and_duplicate_credentials(tmp_path,monkeypatch):
    p,path=pool(tmp_path,monkeypatch)
    path.chmod(0o644)
    with pytest.raises(ValueError,match='0600'): AccountPool(p.db,None,MODEL)
    path.chmod(0o600);data=json.loads(path.read_text());data['accounts'][1]['api_key']=data['accounts'][0]['api_key'];path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Duplicate'): AccountPool(p.db,None,MODEL)


@pytest.mark.parametrize('aliases',[['cursor','b','c'],['a','cursor','c']])
def test_alias_state_cannot_collide_with_routing_cursor(tmp_path,monkeypatch,aliases):
    p,path=pool(tmp_path,monkeypatch,[{'id':a,'api_key':'nace_sk_synthetic_credential_'+a,'prepaid_only':True} for a in aliases])
    assert [p.choose()[1] for _ in range(3)]==aliases
    p.failed('cursor',KairosError('billing','no credit',402))
    reopened=AccountPool(p.db,None,MODEL)
    assert 'cursor' not in [reopened.choose()[1] for _ in range(4)]
    assert next(a for a in p.status() if a['id']=='cursor')['state']=='BLOCKED'
