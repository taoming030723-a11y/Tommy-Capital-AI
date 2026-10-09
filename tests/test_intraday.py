"""Guard complete-bar boundaries and distinguish missing data from no signal."""
from datetime import date, datetime
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tommy_capital import technical
from tommy_capital.cli import run, DEFAULTS, quote_clock_check
from tommy_capital.data import SHANGHAI, DataError, daily_history
from tommy_capital.strategy import evaluate
from test_core import good_row, good_technical, candles


@pytest.mark.parametrize('clock,period,expected', [
    ('2026-10-08 09:40', 15, '2026-09-30 15:00'),
    ('2026-10-08 09:45:20', 15, '2026-09-30 15:00'),
    ('2026-10-08 09:50', 15, '2026-10-08 09:45'),
    ('2026-10-08 12:40', 15, '2026-10-08 11:30'),
    ('2026-10-08 13:10', 15, '2026-10-08 11:30'),
    ('2026-10-08 09:50', 60, '2026-09-30 15:00'),
    ('2026-10-08 10:31', 60, '2026-10-08 10:30'),
])
def test_intraday_cutoff_respects_holiday_lunch_and_completion(clock, period, expected):
    calendar = pd.DataFrame({'trade_date': ['2026-09-30', '2026-10-08', '2026-12-31']})
    now = pd.Timestamp(clock).to_pydatetime().replace(tzinfo=SHANGHAI)
    assert technical.closed_minute_cutoff(calendar, now, period) == pd.Timestamp(expected)


@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
def test_two_confirmed_pivots_produce_both_macd_and_kdj_evidence(monkeypatch, direction):
    frame = candles(60)
    frame['low'] = 100.0
    frame['high'] = 110.0
    frame['close'] = 105.0
    frame.loc[frame.index[52], 'close'] = 100. if direction == 'bullish' else 110.
    price = 'low' if direction == 'bullish' else 'high'
    frame.loc[frame.index[35], price] = 90 if direction == 'bullish' else 120
    frame.loc[frame.index[52], price] = 85 if direction == 'bullish' else 125
    def oscillators(data):
        out = data.copy()
        out['dif'], out['j'], out['atr14'] = 0.0, 50.0, 1.0
        out.loc[out.index[55], 'close'] = 105.
        out.loc[out.index[35], ['dif', 'j']] = [-2, 10] if direction == 'bullish' else [2, 90]
        out.loc[out.index[52], ['dif', 'j']] = [-1, 20] if direction == 'bullish' else [1, 80]
        return out
    monkeypatch.setattr(technical, 'indicators', oscillators)
    signals = technical.confirmed_divergences(frame)['signals']
    assert {s['indicator'] for s in signals} == {'MACD_DIF', 'KDJ_J'}
    assert all(s['direction'] == direction and s['confirmed_at'] == frame.index[55].isoformat() for s in signals)


def test_missing_minute_is_unavailable_not_no_divergence():
    class Missing:
        def fetch(self, *args, **kwargs):
            raise DataError('minute API unavailable')
    actual = technical.minute_observation(Missing(), '600001', pd.Timestamp('2026-10-08 09:45'), candles())
    assert actual['status'] == 'unavailable' and actual['signals'] == []


def test_historical_liquidity_avoids_partial_morning_turnover_cutoff():
    row = {**good_row(), 'turnover': 1000, 'liquidity_deferred': True}
    tech = {**good_technical(), 'average_turnover20_cny': 5e7}
    assert evaluate(row, tech, DEFAULTS, date(2026, 10, 8))['eligible']
    tech['average_turnover20_cny'] = None
    assert not evaluate(row, tech, DEFAULTS, date(2026, 10, 8))['eligible']


def test_stale_intraday_quote_cannot_enter_strategy():
    row = {**good_row(), 'quote_clock_ok': False}
    assert not evaluate(row, good_technical(), DEFAULTS, date(2026,10,8))['eligible']


def test_previous_close_snapshot_is_rejected_after_open(tmp_path, monkeypatch):
    # This is a replay at 09:50, not an acquisition at the test runner's clock.
    monkeypatch.setattr('tommy_capital.cli.quote_clock_check',
                        lambda spot, now, intraday: quote_clock_check(spot, now, intraday, now))
    class OldQuotes:
        lineage = []
        def fetch(self, function, **kwargs):
            if function == 'tool_trade_date_hist_sina':
                return pd.DataFrame({'trade_date': ['2026-09-30','2026-10-08','2026-12-31']})
            assert function == 'spot_sina_full'
            return pd.DataFrame({'代码':['600001'], '名称':['测试'], '最新价':[20], '总市值':[2400],
                                 '成交额':[5e7], '市净率':[2], '市盈率-动态':[20], '行情时刻':['15:00:00']})
    args = SimpleNamespace(config=None,holdings=None,refresh=False,limit=0,output=str(tmp_path))
    assert run(args,provider=OldQuotes(),now=datetime(2026,10,8,9,50,tzinfo=SHANGHAI)) == 2
    report=json.loads((tmp_path/'report.json').read_text())
    assert report['quote_clock_rejected']==1 and report['status']=='failed'
    assert any('盘中行情时刻异常' in e for e in report['errors'])


def test_closing_provider_update_after_1531_is_not_a_future_quote():
    spot = pd.DataFrame({'quote_clock_time': ['15:00:00', '15:30:02', '15:34:59', '15:35:45',
                                             '14:54:59', '15:41:01', None, 'invalid']})
    now = datetime(2026, 10, 8, 15, 34, tzinfo=SHANGHAI)
    acquired = datetime(2026, 10, 8, 15, 40, tzinfo=SHANGHAI)
    valid, diagnostics = quote_clock_check(spot, now, False, acquired)
    assert valid.tolist() == [True, True, True, True, False, False, False, False]
    assert diagnostics['rejected'] == 4
    assert diagnostics['acquired_at'] == acquired.isoformat()


def test_opening_still_rejects_later_close_update_times():
    spot = pd.DataFrame({'quote_clock_time': ['09:49:59', '15:35:45']})
    now = datetime(2026, 10, 8, 9, 50, tzinfo=SHANGHAI)
    valid, _ = quote_clock_check(spot, now, True, now)
    assert valid.tolist() == [True, False]


def test_failed_report_does_not_present_unchecked_stocks_as_zero_candidates():
    from tommy_capital.reporting import render_markdown
    report = {'selection_rule': 'dual_route_monthly_recovery', 'status': 'failed', 'scan_complete': False,
              'coverage': {'source_universe': 5571, 'universe': 5571}, 'errors': ['收盘报价时刻异常'],
              'generated_at': '2026-10-08T15:34:19+08:00', 'finished_at': '2026-10-08T15:34:39+08:00',
              'session': '2026-10-08', 'minute15_cutoff': '2026-10-08T15:00:00', 'rankings': []}
    text = render_markdown(report)
    assert '候选数量无法判断' in text and '5571只' in text
    assert '目标日线日期（未完成逐股核验）' in text
    assert '共0只' not in text and '没有同时通过' not in text


@pytest.mark.parametrize('primary_date,primary_close', [('2026-09-30', 20), ('2026-10-08', 19)])
def test_closing_stale_or_mismatched_primary_uses_verified_backup(primary_date, primary_close):
    class ProtocolFixture:
        calls = []
        def fetch(self, function, **kwargs):
            self.calls.append(function)
            assert kwargs['adjust'] == 'qfq' and kwargs['ttl'] == 60
            day, close = (primary_date, primary_close) if function == 'daily_tx_recent' else ('2026-10-08', 20)
            return pd.DataFrame({'日期': [day], '收盘': [close]})
    source = ProtocolFixture()
    frame, warning = daily_history(source, '600001', '20180101', '20261008', '20261008',
                                  required_session=date(2026, 10, 8), expected_close=20)
    assert source.calls == ['daily_tx_recent', 'stock_zh_a_hist']
    assert frame['日期'].tolist() == ['2026-10-08'] and warning


def test_closing_cannot_fall_back_to_old_daily_data_when_all_sources_are_stale():
    class StaleFixture:
        calls = []
        def fetch(self, function, **kwargs):
            self.calls.append(function)
            return pd.DataFrame({'日期': ['2026-09-30'], '收盘': [20]})
    source = StaleFixture()
    with pytest.raises(DataError, match='三路日线均不可用'):
        daily_history(source, '600001', '20180101', '20261008', '20261008',
                      required_session=date(2026, 10, 8), expected_close=20)
    assert source.calls == ['daily_tx_recent', 'stock_zh_a_hist', 'daily_sina_adjusted']


def test_early_open_fails_before_requesting_market_data(tmp_path):
    class CalendarOnly:
        lineage = []
        def fetch(self, function, **kwargs):
            assert function == 'tool_trade_date_hist_sina'
            return pd.DataFrame({'trade_date': ['2026-09-30', '2026-10-08', '2026-12-31']})
    args = SimpleNamespace(config=None, holdings=None, refresh=False, limit=0, output=str(tmp_path))
    assert run(args, provider=CalendarOnly(), now=datetime(2026, 10, 8, 9, 40, tzinfo=SHANGHAI)) == 2
    report = json.loads((tmp_path/'report.json').read_text())
    assert report['status'] == 'failed' and not report['scan_complete']


def test_minute_adjustment_discards_unfinished_bar_and_removes_split_jump():
    dates = pd.to_datetime(['2026-09-30 15:00', '2026-10-08 09:45', '2026-10-08 10:00'])
    raw = pd.DataFrame({'时间': dates, '开盘':[100.,50.,51.], '收盘':[100.,50.,51.],
                        '最高':[100.,50.,51.], '最低':[100.,50.,51.], '成交量':[100]*3})
    raw_daily = pd.DataFrame({'日期':['2026-09-30'], '开盘':[100], '收盘':[100],
                              '最高':[100], '最低':[100], '成交量':[100]})
    class Source:
        def fetch(self, function, **kwargs):
            if function == 'minute_sina_raw':
                return raw
            assert kwargs['anchor_date'] == '20261008'
            return raw_daily
    qfq = technical.bars(raw_daily)
    qfq[['open', 'close', 'high', 'low']] /= 2
    actual = technical.minute_frame(Source(), '600001', 15, dates[1], qfq)
    assert len(actual) == 2 and actual.close.tolist() == [50., 50.]


def test_closing_minute_adjustment_checks_current_complete_raw_daily():
    day = pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None).normalize()
    times = pd.DatetimeIndex([day + pd.Timedelta(hours=14, minutes=45), day + pd.Timedelta(hours=15)])
    raw = pd.DataFrame({'时间': times, '开盘': [10, 10], '收盘': [10, 10],
                        '最高': [11, 11], '最低': [9, 9], '成交量': [100, 100]})
    daily = pd.DataFrame({'日期': [day], '开盘': [10], '收盘': [10], '最高': [11], '最低': [9], '成交量': [200]})
    class Source:
        def fetch(self, function, **kwargs):
            if function == 'minute_sina_raw':
                return raw
            assert function == 'daily_tx_recent' and kwargs['adjust'] == ''
            assert kwargs['include_current_session'] and kwargs['ttl'] == 60
            return daily
    actual = technical.minute_frame(Source(), '600001', 15, times[-1], technical.bars(daily))
    assert actual.close.tolist() == [10, 10] and actual.index[-1] == times[-1]


def test_completed_holiday_week_is_included_after_the_calendar_week_ends():
    frame=candles(700)
    frame.index=pd.bdate_range(end='2026-09-30',periods=700)
    result=technical.strategic_technical(frame,date(2026,9,30),date(2026,10,7))
    assert result['daily_bar_date']=='2026-09-30'
    assert result['weekly_bar_date']=='2026-10-02'
    assert result['monthly_bar_date']=='2026-09-30'


def test_friday_intraday_does_not_include_unfinished_current_week():
    frame=candles(700)
    frame.index=pd.bdate_range(end='2026-10-08',periods=700)
    result=technical.strategic_technical(frame,date(2026,10,8),date(2026,10,8))
    assert result['weekly_bar_date']=='2026-10-02'
