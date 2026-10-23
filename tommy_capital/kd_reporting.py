"""Independent K/D table and trace, shared by formal reports and local monitor."""
from .kd_state import PERIODS, PERIOD_NAMES


def render_kd_rows(rows):
    from .reporting import table, number, cell
    if not any(row.get("kd_monitor") for row in rows):
        return []
    lines = ["### 多周期K/D状态观察", "",
             "仅完整K线；本模块不计分、不改变买点或做T资格。J=3K−2D，与K−D不重复加分；120/60和30/15按相关组观察，不按信号个数叠加确认。", ""]
    lines += table(["股票", *[PERIOD_NAMES[p] for p in PERIODS], "综合观察"], [
        [str(row.get("name") or row["code"])+"（"+row["code"]+"）",
         *[row.get("kd_monitor", {}).get("frames", {}).get(p, {}).get("label", "数据不足，无法判断") for p in PERIODS],
         row.get("kd_monitor", {}).get("label", "未生成")] for row in rows])
    for row in rows:
        monitor = row.get("kd_monitor", {})
        if not monitor:
            continue
        lines += ["**"+cell(row.get("name") or row["code"])+" · K/D依据**", ""]
        for p in PERIODS:
            f = monitor["frames"].get(p, {})
            if f.get("status") != "ok":
                lines += [PERIOD_NAMES[p]+"："+cell(f.get("error") or "数据不足，无法判断")+"。", ""]
                continue
            current, previous = f["current"], f["previous"]
            lines += [PERIOD_NAMES[p]+"："+cell(f["label"])+"；K "+number(previous["k"])+"→"+number(current["k"])+
                      "，D "+number(previous["d"])+"→"+number(current["d"])+"，K−D "+number(previous["gap"])+"→"+number(current["gap"])+
                      "；完整K线"+cell(f["last_bar"])+("；交叉间距小，勿当强确认" if f["weak_cross"] else "")+"。", ""]
    return lines
