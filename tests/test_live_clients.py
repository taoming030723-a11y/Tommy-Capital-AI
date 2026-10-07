import copy
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
