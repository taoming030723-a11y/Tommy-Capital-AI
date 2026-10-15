"""Download only public watch inputs at a pinned commit; never upload local positions."""
import json
from pathlib import Path

import requests

from .data import DataError
from .monitor import atomic_json
from .tracking import validate_history,validate_preferences,closing_record


def sync_public_inputs(directory,repository="taoming030723-a11y/Tommy-Capital-AI"):
    try:
        response=requests.get(f"https://api.github.com/repos/{repository}/git/ref/heads/main",timeout=(8,20))
        response.raise_for_status();commit=response.json()["object"]["sha"]
        values={}
        for path in ("results/top10-history.json","operations/watchlist-preferences.json","results/latest-summary.json"):
            response=requests.get(f"https://raw.githubusercontent.com/{repository}/{commit}/{path}",timeout=(8,20))
            response.raise_for_status();values[path]=response.json()
        history=validate_history(values["results/top10-history.json"])
        preferences=validate_preferences(values["operations/watchlist-preferences.json"])
        summary=values["results/latest-summary.json"]
        record=closing_record(summary)
        if record is None or record not in history["sessions"]:
            raise ValueError("公开摘要与正式累计历史不一致，保留本机原公开输入")
        root=Path(directory)/commit
        atomic_json(root/"top10-history.json",history)
        atomic_json(root/"latest-summary.json",summary)
        atomic_json(root/"watchlist-preferences.json",preferences)
        atomic_json(root/"source.json",{"repository":repository,"commit":commit})
        return root,commit
    except (requests.RequestException,ValueError,KeyError) as exc:
        raise DataError("公开观察名单同步失败（未上传本机持仓）："+str(exc)) from exc
