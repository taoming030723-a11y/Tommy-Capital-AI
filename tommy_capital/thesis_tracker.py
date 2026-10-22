"""Local research journal, revision guarded and separate from selection/holdings."""
import json
import uuid
from datetime import datetime
from pathlib import Path
from .data import SHANGHAI
from .positions import locked, code_value


def load_theses(path=".tommy/theses.json"):
    file = Path(path)
    return json.loads(file.read_text()) if file.exists() else {"schema": 1, "revision": None, "companies": {}, "events": []}


def save_thesis(code, thesis, confirm_conditions, invalidate_conditions, next_review, path=".tommy/theses.json", expected_revision=None):
    code = code_value(code)
    if not all(isinstance(s, str) and s.strip() and len(s) <= 6000 for s in (thesis, confirm_conditions, invalidate_conditions)):
        raise ValueError("投资理由、验证条件及失效条件均须明确填写")
    if next_review:
        datetime.fromisoformat(next_review)
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with locked(file):
        current = load_theses(file)
        if current["revision"] != expected_revision:
            raise ValueError("投资假设已被另一窗口修改，请重新加载")
        now = datetime.now(SHANGHAI).isoformat()
        record = {"code": code, "thesis": thesis, "confirm_conditions": confirm_conditions,
                  "invalidate_conditions": invalidate_conditions, "next_review": next_review,
                  "updated_at": now, "evidence_status": "user_hypothesis_not_verified_fact"}
        current["companies"][code] = record
        current["events"].append({"event_id": uuid.uuid4().hex, "at": now, "record": record})
        current["revision"] = uuid.uuid4().hex
        temporary = file.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2))
        temporary.chmod(0o600)
        temporary.replace(file)
    return current
