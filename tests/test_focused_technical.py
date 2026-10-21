"""Frozen cross-runtime parity: the new web observer must match the Python math."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tommy_capital.technical import indicators, confirmed_divergences
from tommy_capital.patterns import weekly_ma55_observation
from tommy_capital.cli import clean, make_summary
from tommy_capital.reporting import render_markdown
from test_watchlist_preferences import report_with_watch

ROOT = Path(__file__).resolve().parents[1]


def test_web_macd_kdj_moving_averages_and_strict_pivots_match_frozen_python_history():
    if not shutil.which("node"):
        pytest.skip("Node required for cross-runtime parity")
    rng = np.random.default_rng(20261021)
    close = 20 + np.cumsum(rng.normal(.015, .3, 420))
    frame = pd.DataFrame({"open": close, "close": close, "high": close + .5,
                          "low": close - .5, "volume": rng.integers(100, 1000, 420)},
                         index=pd.bdate_range("2024-01-02", periods=420))
    rows = [{"date": t.isoformat(), **row.to_dict()} for t, row in frame.iterrows()]
    script = """
    import {calculateIndicators,strictDivergences,weeklyMA55} from './web-report/lib/technical-review.mjs';
    let input='';for await (const data of process.stdin) input+=data;
    const calc=calculateIndicators(JSON.parse(input));
    console.log(JSON.stringify({last:calc.at(-1),signals:strictDivergences(calc),ma55:weeklyMA55(calc)}));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                            input=json.dumps(rows), capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    expected = indicators(frame)
    for key, name in (("dif", "dif"), ("dea", "dea"), ("histogram", "macd"),
                      ("atr14", "atr14"), ("k", "k"), ("d", "d"), ("j", "j")):
        assert actual["last"][key] == pytest.approx(float(expected.iloc[-1][name]), abs=1e-10)
    for length in (5, 13, 20, 55, 60, 144, 233):
        assert actual["last"]["ma"][str(length)] == pytest.approx(float(expected.iloc[-1][f"ma{length}"]), abs=1e-10)
    old = confirmed_divergences(frame, window=120)
    for key in ("signals", "weak_signals"):
        core = lambda signals: [(r["direction"], r["indicator"], r["previous_pivot"], r["current_pivot"], r["confirmed_at"]) for r in signals]
        assert core(actual["signals"][key]) == core(old[key])
    ma55 = weekly_ma55_observation(expected)
    for key in ("direction", "above_count", "consecutive_above", "latest_week_above"):
        assert actual["ma55"][key] == ma55[key]
    assert actual["ma55"]["ma55_qfq"] == pytest.approx(ma55["ma55_qfq"])


def test_web_policies_track_original_model_thresholds_without_relaxation():
    for source, web in (("config.json", "technical-policy.json"), ("monitor-config.json", "monitor-policy.json")):
        assert json.loads((ROOT / source).read_text()) == json.loads((ROOT / "web-report/lib" / web).read_text())


def test_focused_report_enrichment_keeps_original_rankings_and_cumulative_history():
    report = report_with_watch()
    original = copy.deepcopy(report)
    report["technical_reviews"] = {"public": True, "schema": 1, "session": report["session"], "rows": [{
        "code": "600000", "name": "冻结测试股", "frames": {}, "decision": "数据未齐，不能确认买点", "unmet_conditions": [{"label": "完整行情"}],
        "data_complete": False, "orders_authorized": False,
    }]}
    summary = clean(make_summary(report))
    assert report["rankings"] == original["rankings"]
    assert report["top10_history"] == original["top10_history"]
    assert summary["technical_reviews"] == report["technical_reviews"]
    markdown = render_markdown(report)
    assert "观察名单六周期技术评估" in markdown and "数据未齐，不能确认买点" in markdown


def test_public_ignore_removes_technical_rows_and_restore_keeps_original_market_facts():
    from tommy_capital.watchlist import technical_preference_view
    row = {"code": "920599", "session": "2026-10-20", "frames": {}, "data_complete": False}
    result = {"public": True, "rows": [row], "session": "2026-10-20"}
    original = copy.deepcopy(result)
    hidden = technical_preference_view(result, {"rows": []})
    assert hidden["rows"] == [] and hidden["all_rows"] == [row]
    restored = technical_preference_view(hidden, {"rows": [{"code": "920599"}]})
    assert restored["rows"] == [row] and restored["session"] == "2026-10-20"
    assert result == original
