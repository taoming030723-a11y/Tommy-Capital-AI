"""Provisional research taxonomy, never a new eligibility or scoring gate."""
from .scoring import finite, financial_sector

METHODS = {
    "stable_earner": ["normalized_profit", "fcff_dcf", "fcfe_dcf"],
    "high_growth": ["normalized_profit", "fcff_dcf", "fcfe_dcf"],
    "cyclical": ["normalized_profit", "ev_sales", "sotp"],
    "loss_reversal": ["normalized_profit", "sotp", "ev_sales"],
    "asset_value": ["sotp"], "financial": ["pb_roe"], "unknown": [],
}
LABELS = dict(zip(METHODS, ["稳定盈利型", "成长研究型", "周期研究型", "亏损反转型", "资产价值型", "金融企业", "类型待核实"]))


def classify_business(row):
    industry = str(row.get("industry") or "")
    profit, growth = finite(row.get("profit_ttm")), finite(row.get("revenue_yoy"))
    if financial_sector(row):
        kind, reason = "financial", "公开行业标签属于金融；需专门资本与资产质量研究"
    elif profit is not None and profit <= 0:
        kind, reason = "loss_reversal", "TTM未盈利；是否持续改善仍以原B路线证据判断"
    elif any(t in industry for t in ("煤炭", "钢铁", "有色", "石油", "化工", "航运", "水泥")):
        kind, reason = "cyclical", "行业具有周期暴露，不能用景气高峰的低PE证明便宜"
    elif finite(row.get("tangible_equity_cny")) is not None and row.get("asset_review_verified") is True:
        kind, reason = "asset_value", "已有可核验有形资产研究记录；仍需负债与减值检查"
    elif profit is not None and profit > 0 and growth is not None:
        kind = "high_growth" if growth >= 20 else "stable_earner"
        reason = "按当前盈利及营收增速分流研究方法，不据此证明增长持久或存在护城河"
    else:
        kind, reason = "unknown", "行业或实际盈利记录不足"
    return {"type": kind, "label": LABELS[kind], "status": "provisional", "reason": reason,
            "preferred_methods": METHODS[kind], "influences_ranking": False}
