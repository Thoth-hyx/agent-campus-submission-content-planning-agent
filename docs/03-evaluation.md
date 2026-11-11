# 测试与评估

本机 Python 3.12.7、Windows；`python -m unittest discover -s tests -v` 实测 **25 项通过**，原始记录见 [test-results.txt](../examples/test-results.txt)。单元测试中的模型 HTTP 交互使用替身响应；下文单列的真实模型记录来自实际网络调用。

## 真实模型记录

2026-11-11 使用项目 `.env` 配置的 DeepSeek 官方兼容接口运行，响应中的模型标识为 `deepseek-flash`。以下各场景使用独立状态库；素材、脚本、费用和故障均由本地工具模拟。

| 场景 | 实际输出 | Trace | 观察到的模型动作 |
| --- | --- | --- | --- |
| 正常，预算 12 | [输入](../examples/normal.input.json) / [输出](../examples/live-normal.output.json) | [Trace](../examples/live-normal.trace.json) | 检索素材、生成常规脚本、估算 12 并保存 |
| 注入脚本失败，预算 12 | [输入](../examples/normal.input.json) / [输出](../examples/live-failure.output.json) | [Trace](../examples/live-failure.trace.json) | 常规模板失败后选择备用模板；已检索素材保留，估算 12 并保存 |
| 超预算，预算 10 | [输入](../examples/over-budget.input.json) / [输出](../examples/live-over-budget.output.json) | [Trace](../examples/live-over-budget.trace.json) | 观察高质量方案 12 超限，选择基础方案，重新估算 8 并保存 |

真实调用说明模型在**这些实测样例**上根据不同工具结果采取了不同动作，并不能保证其他输入或供应商下同样成功。[DeepSeek 官方文档](https://api-docs.deepseek.com/)说明该模型别名当前对应的后端版本；本项目仅记录接口响应的模型标识，未独立验证后端版本。

## 离线验收记录

| 场景 | 输入与实际输出 | Trace | 实测结论 |
| --- | --- | --- | --- |
| 正常 | [输入](../examples/normal.input.json) / [输出](../examples/normal.output.json) | [Trace](../examples/normal.trace.json) | Mock 完成，高质量方案 12 |
| 缺预算 | [输入](../examples/missing-budget.input.json) / [输出](../examples/missing-budget.output.json) | [Trace](../examples/missing-budget.trace.json) | `needs_input`，没有调用模型或工具 |
| 缺截止时间 | [输入](../examples/missing-deadline.input.json) / [输出](../examples/missing-deadline.output.json) | [Trace](../examples/missing-deadline.trace.json) | `needs_input`，没有调用模型或工具 |
| 脚本工具失败 | [输入](../examples/tool-failure.input.json) / [输出](../examples/tool-failure.output.json) | [Trace](../examples/tool-failure.trace.json) | 一次失败后 Mock 选择备用模板；原素材保留，最终完成 |
| 超预算改选 | [输入](../examples/over-budget.input.json) / [输出](../examples/over-budget.output.json) | [Trace](../examples/over-budget.trace.json) | 预算 10，先估算 12，再估算并保存 8 |
| 无可行方案 | [输入](../examples/no-feasible.input.json) / [输出](../examples/no-feasible.output.json) | [Trace](../examples/no-feasible.trace.json) | 预算 5，最低已估 8，缺口 3，未保存 |
| 部分成功 | 使用超预算输入 / [输出](../examples/partial.output.json) | [Trace](../examples/partial.trace.json) | 检索先 `partial` 再成功；部分结果保留 |
| 重复执行 | 使用正常输入 / [输出](../examples/repeat.output.json) | [Trace](../examples/repeat.trace.json) | `reused: true`，返回同一方案和原调用次数 |
| 失败后续跑 | 使用失败输入 / [中断输出](../examples/resume-before.output.json) / [续跑输出](../examples/resume-after.output.json) | [中断 Trace](../examples/resume-before.trace.json) / [续跑 Trace](../examples/resume-after.trace.json) | 搜索未重复，失败尝试保留，续跑完成 |

自动化测试还覆盖截止时间不足、无素材、预算为零、越权保存不同素材、超预算保存、非法参数、并行工具调用、调用上限、真实/Mock 状态隔离，以及真实接口响应与观察传递的模拟测试。

## 失败、未完成与边界

- 初次测试在 Windows 沙箱的默认临时目录及 `tempfile.TemporaryDirectory` 上遇到权限错误；改为项目 `.runtime/tests` 内的独立临时文件后，上述测试通过。
- 首次真实调用因沙箱网络限制失败；获准联网后从保存状态续跑成功，见 [输出](../examples/live-network-recovery.output.json)和 [Trace](../examples/live-network-recovery.trace.json)。该记录包含一次失败的模型请求，不应当计为工具成功。
- 模型可能一次请求多个工具，执行器拒绝该轮调用并回传错误；联调时曾观察到模型随后改为单个动作。实现随后补充了对被拒绝调用参数的记录，并重新运行上述真实场景。
- 模拟 HTTP 测试确认工具消息能回传和业务守卫生效；真实运行提供了有限的端到端证据，无法保证模型对所有输入均能成功重规划。
- 截止日期示例设为 2099 年以便长期复现，不代表实际活动排期。费用、耗时、素材和脚本均为合成数据。
- 最终检查了交付代码、文档与示例输出，未发现与本地配置密钥一致的内容；`.env` 已加入 `.gitignore`。
