"""OpenAI-compatible tool calls and an explicitly offline mock policy."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SYSTEM = """你是内容活动规划 Agent。一次只调用一个工具；每次调用填写一句面向用户的简短 reason，不要输出内部思考过程。
目标是在预算和截止时间内完成方案。先检索素材，生成通用脚本，然后对候选素材估算并保存可行方案。
优先考虑高质量素材，但必须观察估算结果；若超预算、工具失败或结果不完整，应基于观察自行选择替代、再次检索、继续或结束。
估算结果中的 spent 是已发生的模拟费用，remaining 是将来费用，不要重复计算；费用单位为“模拟单位”，不要写成“元”。真实模型 API 费用不在模拟预算中。
已经成功的结果可以复用；保存前必须对当前素材取得有效估算。无可行方案或无法继续时调用 finish 并说明原因。
工具调用不等于执行成功，必须观察返回状态。请勿编造素材或声称未保存的方案已经完成。"""


def schema(name, description, properties, required):
    fields = {"reason": {"type": "string", "description": "一句面向用户的动作依据"}, **properties}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": fields, "required": ["reason", *required], "additionalProperties": False,
    }}}


TOOLS = [
    schema("search_materials", "搜索与主题匹配的合成素材，可能返回部分结果或失败", {}, []),
    schema("generate_script", "基于主题生成可复用的通用活动脚本", {
        "variant": {"type": "string", "enum": ["standard", "backup"], "description": "常规或备用脚本模板"},
    }, ["variant"]),
    schema("estimate_cost", "估算当前素材方案总费用与预计完成时间", {
        "material_id": {"type": "string", "description": "已检索素材的 ID"},
    }, ["material_id"]),
    schema("save_plan", "保存当前已估算且预算、时间均可行的方案", {
        "material_id": {"type": "string", "description": "已估算的素材 ID"},
    }, ["material_id"]),
    schema("finish", "无法继续或没有可行方案时结束任务", {}, []),
]

DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class ModelError(Exception):
    pass


def read_config(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ModelError("无法读取项目 .env 配置文件，请检查权限和 UTF-8 编码") from exc
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ModelError(f".env 第 {number} 行缺少等号")
        key, value = key.strip(), value.strip()
        if key not in ("API_KEY", "MODEL_ID", "BASE_URL"):
            continue
        if value and value[0] in ("\"", "'"):
            if len(value) < 2 or value[-1] != value[0]:
                raise ModelError(f".env 第 {number} 行引号不匹配")
            value = value[1:-1].strip()
        values[key] = value
    return values


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveModel:
    def __init__(self):
        config = read_config(DOTENV_PATH)
        self.base = os.environ.get("MODEL_BASE_URL", config.get("BASE_URL", "")).strip().rstrip("/")
        self.api_key = os.environ.get("MODEL_API_KEY", config.get("API_KEY", "")).strip()
        self.model = os.environ.get("MODEL_NAME", config.get("MODEL_ID", "")).strip()
        missing = [name for name, value in (("MODEL_BASE_URL / BASE_URL", self.base), ("MODEL_API_KEY / API_KEY", self.api_key), ("MODEL_NAME / MODEL_ID", self.model)) if not value or value == "sk-..."]
        if missing:
            raise ModelError("缺少有效模型配置：" + ", ".join(missing) + "；请检查项目 .env 或环境变量")
        parsed = urlsplit(self.base)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("MODEL_BASE_URL 必须是有效的 HTTP(S) 接口地址，不能包含凭据、查询参数或片段")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ModelError("非本机模型接口必须使用 HTTPS")
        self.host = parsed.hostname
        self.last_response_model = None
        self.url = self.base if self.base.endswith("/chat/completions") else self.base + "/chat/completions"

    def complete(self, messages, state):
        payload = {"model": self.model, "messages": messages, "tools": TOOLS, "tool_choice": "auto", "stream": False}
        request = Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST", headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
        })
        try:
            with build_opener(NoRedirect).open(request, timeout=40) as response:
                raw = json.loads(response.read().decode("utf-8"))
            message = raw["choices"][0]["message"]
            self.last_response_model = raw.get("model") if isinstance(raw.get("model"), str) else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("invalid assistant message")
            return message
        except HTTPError as exc:
            raise ModelError(f"模型接口返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ModelError("模型接口连接失败或超时，请检查网络与接口地址") from exc
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelError("模型接口返回了无法解析的响应") from exc


class MockModel:
    """A deterministic test policy, not evidence of an LLM's decision quality."""

    def complete(self, messages, state):
        steps = state["steps"]
        materials = list(state["materials"].values())
        materials.sort(key=lambda item: (item["tier"] != "premium", item["id"]))
        last_search = steps["search_materials"]
        if last_search["status"] in ("pending", "partial"):
            name, args = "search_materials", {"reason": "需要获取主题素材及可用替代项"}
        elif last_search["status"] == "failed":
            code = last_search["attempts"][-1]["result"].get("code")
            name, args = ("search_materials", {"reason": "检索故障后再次获取素材"}) if code == "injected_failure" else ("finish", {"reason": "素材检索无法继续"})
        elif not materials:
            name, args = "finish", {"reason": "没有检索到主题素材"}
        elif not state.get("script"):
            step = steps["generate_script"]
            code = step["attempts"][-1]["result"].get("code") if step["attempts"] else None
            if code and code != "injected_failure":
                name, args = "finish", {"reason": "脚本生成无法继续：" + code}
            else:
                variant = "backup" if code == "injected_failure" else "standard"
                why = "常规脚本工具失败，尝试备用模板" if code else "生成可复用的通用活动脚本"
                name, args = "generate_script", {"reason": why, "variant": variant}
        else:
            quote = state.get("latest_quote")
            if quote and quote["fits_budget"] and quote["fits_deadline"]:
                save_step = steps["save_plan"]
                last_code = save_step["attempts"][-1]["result"].get("code") if save_step["attempts"] else None
                if last_code and last_code != "injected_failure":
                    name, args = "finish", {"reason": "方案保存受阻：" + last_code}
                else:
                    name, args = "save_plan", {"reason": "最新估算满足预算与截止时间", "material_id": quote["material_id"]}
            else:
                remaining = [m for m in materials if m["id"] not in state["quotes"]]
                if remaining:
                    name = "estimate_cost"
                    args = {"reason": "估算候选方案" if not quote else "当前方案不可行，估算另一种素材", "material_id": remaining[0]["id"]}
                else:
                    name, args = "finish", {"reason": "已估算候选方案均不满足预算或截止时间"}
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"mock-{len(state['trace']) + 1}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }]}
