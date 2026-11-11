# 内容任务规划 Agent

输入活动主题、预算和截止时间，Agent 选择工具并观察结果，生成和保存预算、时间可行的内容方案。业务素材和工具为本地模拟；支持真实模型和离线 Mock。

## 运行

需要 Python 3.10+，无需安装第三方依赖。在项目根目录运行：

```powershell
python -m src.main --input examples/normal.input.json --mode mock
python -m src.main --input examples/over-budget.input.json --mode mock
python -m src.main --input examples/tool-failure.input.json --mode mock --fail-once generate_script
python -m unittest discover -s tests -v
```

输入示例：`{"topic": "绿色出行", "budget": 10, "deadline": "2099-12-31"}`。预算为 0 至 10 亿的模拟单位，最多两位小数；截止时间支持日期或带时区的 ISO 时间，纯日期按北京时间当天结束处理。

使用真实模型时，复制 `.env.example` 为 `.env`，在本机填写 `API_KEY`、`MODEL_ID` 和 `BASE_URL`，然后运行：

```powershell
python -m src.main --input examples/normal.input.json --mode live --state .runtime/live.db
```

接口须支持 OpenAI 兼容的 Chat Completions 工具调用；真实 API 费用不计入模拟预算。`.env` 不应提交，进程环境变量 `MODEL_API_KEY`、`MODEL_NAME`、`MODEL_BASE_URL` 可覆盖文件配置。

## 方案与验证

- 模型选择动作 → 工具执行 → 返回结果 → 模型调整或结束；保存前校验素材、脚本、估算、预算及截止时间，每次运行模型和工具各最多调用 20 次。
- SQLite 保存步骤和结果；相同任务重复执行时复用已保存方案，未完成任务可用 `--resume` 续跑。更换 `--state` 可开始独立运行。
- 25 项自动化测试通过。真实 `deepseek-flash` 记录覆盖[正常完成](examples/live-normal.trace.json)、[工具失败后改用备用模板](examples/live-failure.trace.json)和[预算 10 时由 12 改选 8](examples/live-over-budget.trace.json)。
- 输出包含步骤状态、工具结果、模拟费用、保存方案或未完成原因及 Trace。示例日期设置为 2099 年仅为便于复现。

完整的[需求](docs/01-requirements.md)、[设计](docs/02-solution.md)、[评估与用例](docs/03-evaluation.md)、[AI 使用](docs/04-ai-usage.md)和[复盘](docs/05-retrospective.md)见 `docs/`；`examples/` 保存实际输入、输出和 Trace。

## 限制

离线 Mock 只验证流程；真实模型结果仅覆盖所记录的样例。素材、脚本、费用和耗时均为合成数据，输出需人工审核；SQLite 实现未针对并发和生产环境设计。
