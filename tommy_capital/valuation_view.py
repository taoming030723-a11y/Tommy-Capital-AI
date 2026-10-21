"""Render separate valuation observations without recomputing research decisions."""
import argparse
import json
from pathlib import Path

from .cli import clean, make_summary, write_outputs


def refresh(directory):
    root = Path(directory)
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    write_outputs(root, report)
    (root / "summary.json").write_text(
        json.dumps(clean(make_summary(report)), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description="附加估值后的同源报告渲染，不重新评分或抓取行情")
    parser.add_argument("--out", required=True)
    refresh(parser.parse_args().out)


if __name__ == "__main__":
    main()
