"""Bounded model -> tool -> observation loop with resumable local state."""

import copy
import json
from datetime import datetime, timedelta

from .domain import Store, TOOL_NAMES, catalog, task_id, utcnow, validate_input
from .models import LiveModel, MockModel, ModelError, SYSTEM
from .tools import do_tool, incomplete_details, update_step


ARGUMENTS = {
    "search_materials": {"reason"}, "generate_script": {"reason", "variant"},
    "estimate_cost": {"reason", "material_id"}, "save_plan": {"reason", "material_id"}, "finish": {"reason"},
}


def new_state(request, now, mode):
    return {
        "task_id": task_id(request, mode), "request": request, "mode": mode, "status": "running", "reason": "",
        "started_at": now.isoformat(), "simulated_at": now.isoformat(), "spent": "0.00",
        "steps": {name: {"status": "pending", "attempts": []} for name in TOOL_NAMES},
        "materials": {}, "script": None, "quotes": {}, "latest_quote": None, "plan": None,
        "trace": [], "model_calls_total": 0, "tool_calls_total": 0, "injected_failures": [], "injected_partials": [],
    }


def result_view(state, reused=False):
    result = copy.deepcopy(state)
    result.pop("injected_failures", None)
    result.pop("injected_partials", None)
    result["reused"] = reused
    result["model_api_cost_included"] = False
    if result["status"] == "incomplete":
        result["incomplete_details"] = incomplete_details(state)
    return result


def validate_action(name, raw):
    if not isinstance(name, str) or name not in ARGUMENTS:
        return None, "未知工具"
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return None, "工具参数不是有效的 JSON 对象"
    if not isinstance(args, dict) or set(args) != ARGUMENTS[name]:
        return None, "工具参数缺失或包含未定义字段"
    if not isinstance(args["reason"], str) or not args["reason"].strip() or len(args["reason"]) > 500:
        return None, "reason 必须是 1 到 500 字的简短说明"
    if name == "generate_script" and args["variant"] not in ("standard", "backup"):
        return None, "脚本模板必须为 standard 或 backup"
    if name in ("estimate_cost", "save_plan") and (not isinstance(args["material_id"], str) or not args["material_id"]):
        return None, "material_id 必须是非空字符串"
    return args, None


def context_message(state, data):
    return json.dumps({
        "request": state["request"], "simulated_at": state["simulated_at"], "spent": state["spent"],
        "tool_costs_and_minutes": data["tools"], "materials": list(state["materials"].values()),
        "script": state["script"], "quotes": list(state["quotes"].values()), "latest_quote": state["latest_quote"],
        "step_status_and_attempts": state["steps"],
    }, ensure_ascii=False)


def run(data_input, db_path, mode, *, resume=False, fail_once=None, partial_once=None, max_calls=20, now=utcnow, model_override=None):
    request, error = validate_input(data_input)
    if error:
        return {**error, "steps": {}, "trace": [], "model_calls_total": 0, "tool_calls_total": 0}
    if mode not in ("mock", "live") or not 1 <= max_calls <= 20:
        raise ValueError("mode 或调用上限无效")
    store = Store(db_path)
    try:
        key = task_id(request, mode)
        state = store.get(key)
        if state and state["status"] == "completed":
            result = result_view(state, reused=True)
            current = max(now(), datetime.fromisoformat(state["simulated_at"]))
            finish = current + timedelta(minutes=catalog()["tools"]["editing"]["minutes"])
            result["revalidated_estimated_finish"] = finish.isoformat()
            result["deadline_still_feasible"] = finish <= datetime.fromisoformat(request["deadline"])
            if not result["deadline_still_feasible"]:
                result["warning"] = "返回历史已保存方案；按当前时间预计无法在截止前完成剩余工作"
            return result
        if state and not resume:
            result = result_view(state, reused=True)
            if result["status"] == "running":
                result["status"] = "incomplete"
                result["reason"] = "上次运行未正常结束，可使用 --resume 续跑"
            result["resume_available"] = True
            return result
        if not state:
            state = new_state(request, now(), mode)
            if now() > datetime.fromisoformat(request["deadline"]):
                state["status"] = "incomplete"
                state["reason"] = "截止时间已过"
                store.put(state)
                return result_view(state)
        state["status"] = "running"
        state["reason"] = ""
        state["trace"].append({"event": "run_started", "mode": mode, "resumed": resume, "time": now().isoformat()})
        store.put(state)
        data = catalog()
        try:
            model = model_override or (MockModel() if mode == "mock" else LiveModel())
        except ModelError as exc:
            state["status"], state["reason"] = "incomplete", str(exc)
            state["trace"].append({"event": "model_error", "mode": mode, "reason": str(exc)})
            store.put(state)
            return result_view(state)

        if isinstance(model, LiveModel):
            state["trace"].append({"event": "model_configured", "mode": mode, "requested_model": model.model, "base_host": model.host})
            store.put(state)
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": context_message(state, data)}]
        model_calls = tool_calls = 0
        while state["status"] == "running":
            if model_calls >= max_calls or tool_calls >= max_calls:
                state["status"], state["reason"] = "incomplete", f"达到本次模型或工具调用上限（{max_calls}），可续跑"
                break
            try:
                model_calls += 1
                state["model_calls_total"] += 1
                message = model.complete(messages, state)
                if isinstance(model, LiveModel):
                    state["trace"].append({"event": "model_response", "mode": mode, "reported_model": model.last_response_model})
            except ModelError as exc:
                state["status"], state["reason"] = "incomplete", str(exc)
                state["trace"].append({"event": "model_error", "mode": mode, "reason": str(exc)})
                break
            if not isinstance(message, dict):
                state["status"], state["reason"] = "incomplete", "模型响应格式无效"
                break
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                state["status"], state["reason"] = "incomplete", "模型工具调用格式无效"
                break
            if not calls:
                text = message.get("content")
                state["status"] = "incomplete"
                state["reason"] = ("模型结束但尚未保存方案：" + text[:500]) if isinstance(text, str) and text else "模型结束但尚未保存方案"
                break
            if any(not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] for call in calls):
                state["status"], state["reason"] = "incomplete", "模型工具调用缺少有效 ID"
                break
            # Preserve provider-specific fields in memory for the next call, including DeepSeek's reasoning_content.
            messages.append({key: message[key] for key in ("role", "content", "tool_calls", "reasoning_content") if key in message})
            if len(calls) > 1:
                if tool_calls + len(calls) > max_calls:
                    state["status"], state["reason"] = "incomplete", f"并行工具请求超过调用上限（{max_calls}）"
                    break
                for call in calls:
                    tool_calls += 1
                    state["tool_calls_total"] += 1
                    observation = {"status": "failed", "code": "multiple_actions", "reason": "请每次只选择一个动作，观察结果后再决定下一步"}
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = function.get("name", "unknown")
                    name = name if isinstance(name, str) else "unknown"
                    args, _ = validate_action(name, function.get("arguments"))
                    state["trace"].append({"event": "model_action", "mode": mode, "tool": name, "arguments": args or {}, "reason": args.get("reason") if args else "参数校验失败"})
                    state["trace"].append({"event": "tool_result", "mode": mode, "tool": name, "result": observation})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation, ensure_ascii=False)})
                store.put(state)
                continue

            call = calls[0]
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name", "unknown")
            if not isinstance(name, str):
                name = "unknown"
            args, invalid = validate_action(name, function.get("arguments"))
            tool_calls += 1
            state["tool_calls_total"] += 1
            state["trace"].append({"event": "model_action", "mode": mode, "tool": name, "arguments": args if args else {}, "reason": args.get("reason") if args else "参数校验失败"})
            store.put(state)
            if invalid:
                observation = {"status": "failed", "code": "invalid_action", "reason": invalid}
                if name in state["steps"]:
                    update_step(state, name, "failed", observation)
            elif name == "finish":
                state["status"], state["reason"] = "incomplete", args["reason"]
                observation = {"status": "incomplete", "reason": args["reason"], **incomplete_details(state)}
            else:
                observation = do_tool(state, name, args, data, now, fail_once=fail_once, partial_once=partial_once)
            state["trace"].append({"event": "tool_result", "mode": mode, "tool": name, "result": observation, "spent": state["spent"], "simulated_at": state["simulated_at"]})
            store.put(state)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation, ensure_ascii=False)})
        state["trace"].append({"event": "run_finished", "mode": mode, "status": state["status"], "reason": state["reason"]})
        store.put(state)
        return result_view(state)
    finally:
        store.close()
