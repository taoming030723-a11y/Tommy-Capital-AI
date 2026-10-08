"""Isolate each provider call so the parent can enforce a hard deadline."""
import contextlib
import json
import sys


def main():
    request = json.load(sys.stdin)
    allowed = {"stock_zh_a_spot_em", "stock_yjbb_em", "stock_zh_a_hist",
               "stock_zh_a_hist_min_em", "tool_trade_date_hist_sina", "stock_zh_a_hist_tx",
               "finance_em_named", "spot_sina_full", "daily_tx_recent", "minute_sina_raw", "daily_sina_adjusted", "monthly_tx_qfq"}
    if request["function"] not in allowed:
        raise ValueError("Unsupported provider function")
    with contextlib.redirect_stdout(sys.stderr):
        from . import live
        native = {"finance_em_named": live.finance, "spot_sina_full": live.spot_sina,
                  "daily_tx_recent": live.daily_tx, "minute_sina_raw": live.minute_sina,
                  "daily_sina_adjusted": live.daily_sina, "monthly_tx_qfq": live.monthly_tx_qfq}
        if request["function"] in native:
            frame = native[request["function"]](**request["kwargs"])
        else:
            import akshare as ak
            frame = getattr(ak, request["function"])(**request["kwargs"])
    sys.stdout.write(frame.to_json(orient="split", date_format="iso", force_ascii=False))


if __name__ == "__main__":
    main()
