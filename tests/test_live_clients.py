import copy
import json
from types import SimpleNamespace
import pytest
from tommy_capital import live


def record(code, period='2026-06-30'):
    row = {key: 1 for key in live.FIN_FIELDS}
    row.update(SECURITY_CODE=code, SECURITY_NAME_ABBR='测试公司', REPORTDATE=period,
               SECUCODE=code+'.SH', NOTICE_DATE='2026-08-20', UPDATE_DATE='2026-08-20')
    return row


def test_finance_rejects_repeated_pages_and_wrong_period(monkeypatch, tmp_path):
    monkeypatch.setenv('TOMMY_PAGE_CACHE', str(tmp_path))
    a = record('600001')
    monkeypatch.setattr(live, 'get_json', lambda *a,**k: {'code':0,'result':{'pages':2,'count':2,'data':[copy.deepcopy(a_record)]}})
    a_record = a
    with pytest.raises(ValueError, match='重复'):
        live.finance('20260630')
    monkeypatch.setenv('TOMMY_PAGE_CACHE', str(tmp_path/'wrong-period'))
    monkeypatch.setattr(live, 'get_json', lambda *a,**k: {'code':0,'result':{'pages':1,'count':1,'data':[record('600001','2026-03-31')]}})
    with pytest.raises(ValueError, match='报告期'):
        live.finance('20260630')


def test_financial_retry_reuses_successful_pages_without_filling_missing_data(monkeypatch, tmp_path):
    monkeypatch.setenv('TOMMY_PAGE_CACHE', str(tmp_path))
    calls=[]
    def fetch(url, params):
        page=params['pageNumber']
        calls.append(page)
        if page==2 and calls.count(2)==1:
            raise live.requests.ReadTimeout('temporary failure')
        return {'code':0,'result':{'pages':2,'count':2,'data':[record('60000'+str(page))]}}
    monkeypatch.setattr(live,'get_json',fetch)
    with pytest.raises(live.requests.ReadTimeout):
        live.finance('20260630')
    result=live.finance('20260630')
    assert len(result)==2 and calls.count(1)==1 and calls.count(2)==2


def test_qfq_client_does_not_silently_accept_raw_prices(monkeypatch):
    monkeypatch.setattr(live, 'get_json', lambda *a,**k: {'data':{'sh600001':{'day':[['2026-09-30','10','10','10','10','100']]}}})
    with pytest.raises(ValueError,match='qfqday'):
        live.daily_tx('600001','20260930',adjust='qfq')


def sina_responses(monkeypatch, factors):
    """Protocol fixtures validate adjustment; they are never scan inputs."""
    from py_mini_racer import py_mini_racer
    records = [dict(date='2026-09-28',open=20,close=20,high=21,low=19,volume=100,amount=2000),
               dict(date='2026-09-30',open=10,close=10,high=11,low=9,volume=200,amount=2000)]
    class Decoder:
        def eval(self, code): pass
        def call(self, function, encoded): return copy.deepcopy(records)
    monkeypatch.setattr(py_mini_racer, 'MiniRacer', Decoder)
    def request(url, params):
        text = 'var prices="compressed";' if url.endswith('klc_kl.js') else 'var factors=' + json.dumps({'data':factors}) + ';'
        return SimpleNamespace(text=text)
    monkeypatch.setattr(live,'request',request)


def test_sina_adjustment_keeps_only_received_trading_bars(monkeypatch):
    sina_responses(monkeypatch,[['1900-01-01','2'],['2026-09-29','1']])
    frame=live.daily_sina('600001','20260930',anchor_date='20261007')
    assert frame['日期'].dt.strftime('%Y-%m-%d').tolist()==['2026-09-28','2026-09-30']
    assert frame['收盘'].tolist()==[10,10]
    assert frame['成交额'].tolist()==[2000,2000]


@pytest.mark.parametrize('factors', [[], [['2026-09-29','1']], [['1900-01-01','0']],
                                      [['1900-01-01','nan']], [['1900-01-01','1'],['2026-10-08','1']]])
def test_sina_missing_invalid_or_future_factors_are_not_raw_fallback(monkeypatch,factors):
    sina_responses(monkeypatch,factors)
    with pytest.raises(ValueError,match='因子'):
        live.daily_sina('600001','20260930',anchor_date='20261007')


def test_sina_raw_daily_does_not_require_or_invent_factors(monkeypatch):
    sina_responses(monkeypatch,[])
    frame=live.daily_sina('600001','20260930',adjust='')
    assert frame['收盘'].tolist()==[20,10]
