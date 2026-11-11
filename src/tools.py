"""The four local business tools and their budget/deadline guards."""

from datetime import datetime, timedelta
from decimal import Decimal

from .domain import amount, catalog


def current_time(state, now):
    return max(datetime.fromisoformat(state["simulated_at"]), now())


def deadline(state):
    return datetime.fromisoformat(state["request"]["deadline"])


def update_step(state, name, status, result):
    step = state["steps"][name]
    step["attempts"].append({"status": status, "result": result})
    step["status"] = "success" if status == "reused" else status
    if status in ("success", "partial", "reused"):
        step["result"] = result


def failure(state, name, reason, code, extra=None):
    result = {"status": "failed", "code": code, "reason": reason}
    if extra:
        result.update(extra)
    update_step(state, name, "failed", result)
    return result


def do_tool(state, name, args, data, now, fail_once=None, partial_once=None):
    """Execute one simulated tool call; the caller records the model action and result."""
    req = state["request"]
    table = data["tools"]
    if name == "search_materials" and state["steps"][name]["status"] == "success":
        result = {"status": "reused", "materials": list(state["materials"].values())}
        update_step(state, name, "reused", result)
        return result
    if name == "generate_script" and state.get("script"):
        result = {"status": "reused", "script": state["script"]}
        update_step(state, name, "reused", result)
        return result

    if name == "generate_script" and not state["materials"]:
        return failure(state, name, "需要先获得可用素材", "missing_materials")
    if name in ("estimate_cost", "save_plan"):
        if not state.get("script"):
            return failure(state, name, "需要先成功生成脚本", "missing_script")
        if args["material_id"] not in state["materials"]:
            return failure(state, name, "素材未检索到或不可用", "unknown_material")

    clock = current_time(state, now)
    fee = Decimal(table[name]["cost"])
    duration = timedelta(minutes=table[name]["minutes"])
    if Decimal(state["spent"]) + fee > Decimal(req["budget"]):
        gap = Decimal(state["spent"]) + fee - Decimal(req["budget"])
        return failure(state, name, "执行该工具将超过预算", "budget_exceeded", {"budget_gap": amount(gap)})
    if clock + duration > deadline(state):
        return failure(state, name, "执行该工具将超过截止时间", "deadline_exceeded")

    if fail_once == name and name not in state["injected_failures"]:
        state["injected_failures"].append(name)
        state["simulated_at"] = (clock + timedelta(minutes=table["failed_attempt"]["minutes"])).isoformat()
        return failure(state, name, "模拟工具故障，本次未收费", "injected_failure")

    if name == "save_plan":
        quote = state.get("latest_quote")
        if not quote or quote["material_id"] != args["material_id"]:
            return failure(state, name, "保存前必须估算当前素材方案", "missing_current_quote")
        if quote["spent"] != state["spent"] or quote["script_id"] != state["script"]["id"]:
            return failure(state, name, "方案或已发生费用已变化，请重新估算", "stale_quote")
        if not quote["fits_budget"]:
            return failure(state, name, "估算费用超出预算", "budget_exceeded", {"budget_gap": quote["budget_gap"]})
        finish = clock + duration + timedelta(minutes=table["editing"]["minutes"])
        if finish > deadline(state):
            return failure(state, name, "预计无法在截止时间前完成", "deadline_exceeded")

    state["spent"] = amount(Decimal(state["spent"]) + fee)
    state["simulated_at"] = (clock + duration).isoformat()

    if name == "search_materials":
        topic = req["topic"]
        matches = [m for m in data["materials"] if any(tag in topic or (len(topic) >= 2 and topic in tag) for tag in m["tags"])]
        partial = partial_once == name and name not in state["injected_partials"] and bool(matches)
        if partial:
            state["injected_partials"].append(name)
            matches = matches[:1]
        for item in matches:
            state["materials"][item["id"]] = item
        result = {"status": "partial" if partial else "success", "materials": matches}
        if partial:
            result["warning"] = "部分素材源超时，返回已获得的素材"
        update_step(state, name, result["status"], result)
        return result

    if name == "generate_script":
        variant = args["variant"]
        topic = req["topic"]
        text = (
            f"开场：从身边的小事认识{topic}。\n主体：用一个具体场景展示参与方式与活动亮点。\n结尾：邀请观众参与{topic}活动并分享体验。"
            if variant == "standard" else
            f"备用开场：今天介绍{topic}活动。\n步骤：说明活动时间、参与方式和素材画面。\n行动提示：关注活动页面，按指引参与并分享。"
        )
        script = {
            "id": "script-" + variant,
            "variant": variant,
            "content": text,
            "note": "合成示例文案，发布前需人工审核事实和品牌要求。",
        }
        state["script"] = script
        result = {"status": "success", "script": script}
        update_step(state, name, "success", result)
        return result

    if name == "estimate_cost":
        material = state["materials"][args["material_id"]]
        licensing = Decimal(material["license_cost"])
        editing = Decimal(table["editing"]["cost"])
        spent = Decimal(state["spent"])
        total = spent + licensing + editing
        gap = max(total - Decimal(req["budget"]), Decimal("0"))
        finish = datetime.fromisoformat(state["simulated_at"]) + timedelta(minutes=table["save_plan"]["minutes"] + table["editing"]["minutes"])
        quote = {
            "material_id": material["id"], "script_id": state["script"]["id"],
            "spent": amount(spent), "remaining": {"material_license": amount(licensing), "editing": amount(editing)},
            "total": amount(total), "budget": req["budget"], "budget_gap": amount(gap),
            "fits_budget": gap == 0, "estimated_finish": finish.isoformat(), "fits_deadline": finish <= deadline(state),
        }
        state["quotes"][material["id"]] = quote
        state["latest_quote"] = quote
        result = {"status": "success", "estimate": quote}
        update_step(state, name, "success", result)
        return result

    if name == "save_plan":
        quote = state["latest_quote"]
        finish = datetime.fromisoformat(state["simulated_at"]) + timedelta(minutes=table["editing"]["minutes"])
        plan = {
            "plan_id": state["task_id"], "topic": req["topic"], "material": state["materials"][args["material_id"]],
            "script": state["script"], "estimated_cost": quote["total"], "cost_breakdown": {"spent": quote["spent"], **quote["remaining"]},
            "estimated_finish": finish.isoformat(), "deadline": req["deadline"],
            "remaining_work": [{"step": "内容编辑与制作", "status": "pending", "estimated_minutes": table["editing"]["minutes"]}],
        }
        state["plan"] = plan
        state["status"] = "completed"
        result = {"status": "success", "plan_id": plan["plan_id"], "estimated_cost": plan["estimated_cost"]}
        update_step(state, name, "success", result)
        return result
    raise ValueError("unknown tool")


def incomplete_details(state):
    quotes = list(state["quotes"].values())
    if not quotes:
        if state["materials"]:
            table = catalog()["tools"]
            pending_script = Decimal("0") if state["script"] else Decimal(table["generate_script"]["cost"])
            cheapest = min(Decimal(item["license_cost"]) for item in state["materials"].values())
            total = Decimal(state["spent"]) + pending_script + cheapest + Decimal(table["editing"]["cost"])
            gap = max(total - Decimal(state["request"]["budget"]), Decimal("0"))
            return {"lowest_estimated_total": amount(total), "budget_gap": amount(gap),
                    "scope": "基于已检索素材和费用表的理论最低值，尚未逐项估算"}
        return {}
    best = min(quotes, key=lambda item: Decimal(item["total"]))
    return {
        "lowest_estimated_total": best["total"], "budget_gap": best["budget_gap"],
        "evaluated_materials": len(quotes), "retrieved_materials": len(state["materials"]),
        "scope": "全部已检索素材" if len(quotes) == len(state["materials"]) and state["steps"]["search_materials"]["status"] == "success" else "仅已估算素材",
        "deadline_gap_minutes": max(0, int((datetime.fromisoformat(best["estimated_finish"]) - deadline(state)).total_seconds() / 60 + 0.999999)),
    }
