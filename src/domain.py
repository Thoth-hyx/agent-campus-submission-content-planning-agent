"""Input validation, money/time handling, and local task persistence."""

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


CHINA = timezone(timedelta(hours=8))
CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog.json"
TOOL_NAMES = ("search_materials", "generate_script", "estimate_cost", "save_plan")


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("预算必须是非负数字")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("预算必须是非负数字") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000"):
        raise ValueError("预算必须是 0 到 1000000000 之间的有限数字")
    if amount.as_tuple().exponent < -2:
        raise ValueError("预算最多支持两位小数")
    return amount


def amount(value):
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def parse_deadline(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("截止时间必须是日期或带时区的 ISO 时间")
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), time(23, 59, 59), CHINA)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("截止时间格式无效") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("日期时间必须包含时区，例如 +08:00")
    return result


def validate_input(data):
    if not isinstance(data, dict):
        return None, {"status": "invalid_input", "reason": "输入必须是 JSON 对象"}
    required = ("topic", "budget", "deadline")
    missing = [key for key in required if key not in data or data[key] is None or data[key] == ""]
    if missing:
        return None, {"status": "needs_input", "missing_fields": missing, "reason": "请补充必填信息"}
    if not isinstance(data["topic"], str) or not data["topic"].strip():
        return None, {"status": "invalid_input", "reason": "主题必须是非空字符串"}
    try:
        budget = amount(money(data["budget"]))
        deadline = parse_deadline(data["deadline"]).isoformat()
    except (ValueError, InvalidOperation) as exc:
        return None, {"status": "invalid_input", "reason": str(exc)}
    return {"topic": data["topic"].strip(), "budget": budget, "deadline": deadline}, None


def task_id(request, mode):
    raw = json.dumps({"request": request, "mode": mode}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def utcnow():
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.db.commit()

    def get(self, key):
        row = self.db.execute("SELECT state FROM tasks WHERE id = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, state):
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks (id, state) VALUES (?, ?)", (state["task_id"], payload))

    def close(self):
        self.db.close()
