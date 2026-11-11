"""CLI entry point: python -m src.main --input ... --mode mock|live."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .agent import run
from .domain import TOOL_NAMES


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="内容任务规划 Agent")
    parser.add_argument("--input", required=True, help="输入 JSON 文件")
    parser.add_argument("--mode", choices=("mock", "live"), required=True, help="离线模拟或真实模型")
    parser.add_argument("--state", default=".runtime/agent.db", help="SQLite 状态文件")
    parser.add_argument("--resume", action="store_true", help="续跑相同输入的未完成任务")
    parser.add_argument("--fail-once", choices=TOOL_NAMES, help="对指定工具注入一次失败，供验收使用")
    parser.add_argument("--partial-once", choices=("search_materials",), help="对素材检索注入一次部分成功")
    parser.add_argument("--max-calls", type=int, default=20, choices=range(1, 21), metavar="1..20", help="每次运行的模型和工具调用上限")
    args = parser.parse_args()
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        output = run(data, args.state, args.mode, resume=args.resume, fail_once=args.fail_once, partial_once=args.partial_once, max_calls=args.max_calls)
    except sqlite3.Error:
        print(json.dumps({"status": "incomplete", "reason": "状态数据库不可用，请检查路径和权限"}, ensure_ascii=False, indent=2))
        return 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"status": "invalid_input", "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 2 if output["status"] == "invalid_input" else 0


if __name__ == "__main__":
    sys.exit(main())
