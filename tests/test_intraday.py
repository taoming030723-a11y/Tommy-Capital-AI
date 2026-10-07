"""Guard complete-bar boundaries and distinguish missing data from no signal."""
from datetime import date, datetime
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tommy_capital import technical
from tommy_capital.cli import run, DEFAULTS
from tommy_capital.data import SHANGHAI, DataError
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
    price = 'low' if direction == 'bullish' else 'high'
    frame.loc[frame.index[35], price] = 90 if direction == 'bullish' else 120
    frame.loc[frame.index[52], price] = 85 if direction == 'bullish' else 125
    def oscillators(data):
        out = data.copy()
        out['dif'], out['j'] = 0.0, 50.0
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
