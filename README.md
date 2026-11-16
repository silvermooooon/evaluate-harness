# Evaluate Harness

在相同模型、任务、初始数据和预算下，对比不同 Agent Harness 的任务完成质量与执行成本。

本项目复用 [TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany) 的官方任务容器、检查点和评分器，提供本地部署、OpenCode / Agent Runtime SDK 适配器、预算代理、轨迹归档及结果汇总。**当前是本地命令行评估工具，不是完整 TAC 榜单或 Web 服务。**

当前已实现的对照是 **OpenCode v2.2.7 ↔ Duet 所用 Agent Runtime SDK 的单 Agent 装配**，模型固定为 `gpt-6-luna`。本仓库不包含 Duet 产品或私有 SDK 源码；运行 SDK 一侧需要自行提供兼容的 `agent-runtime` 项目。见 [SDK 适配器说明](adapters/duet-notes.md)。

## 已有结果

[首轮本地实验报告](docs/experiment-2026-11.md) · [公开聚合数据](docs/experiment-2026-11.json)

完整且没有连接故障的首轮四题结果：

| 指标 | Agent Runtime SDK | OpenCode |
| --- | ---: | ---: |
| 官方平均分 | 0.646 | 0.521 |
| 完全通过 | 2/4 | 1/4 |
| 平均 API 请求尝试 | 11.75 | 12.50 |
| 平均执行时间 | 65.7 秒 | 96.5 秒 |

这不是总体排名：样本很小，PDF 参考文件和费用舍入检查存在敏感性；第二轮受到上游连接故障影响，恢复计划未完成。公开报告保留这些限制，原始日志、简历与财务数据不随代码发布。

## 评估什么

- **结果质量**：官方完整完成率、官方标准化得分、原始 checkpoint 明细。
- **执行效率**：上游请求尝试数、成功模型响应数、输入/缓存/输出 token、执行耗时。
- **成本与可靠性**：已知用量费用、费用覆盖率、保守预算扣账、连接错误、工具错误和退出原因。
- **运行时诊断**：原生 turn、tool call、compaction 计数。不同 Harness 对这些事件的定义不同，单独列示。

评分保持 TAC 的公式：

```text
score = 0.5 × (final_score.result / final_score.total)
      + 0.5 × 完整完成
```

`final_score` 已应用任务自己的官方聚合策略。效率指标不加权混入新的总分。参见 [官方汇总代码](https://github.com/TheAgentCompany/TheAgentCompany/blob/98b68ef82a47690c316f42fddb05baafaab56851/evaluation/summarise_results.py) 和 [评分策略](https://github.com/TheAgentCompany/TheAgentCompany/blob/98b68ef82a47690c316f42fddb05baafaab56851/workspaces/base_image/scoring.py)。

## 快速开始

已验证环境：macOS ARM64、Docker Desktop、Linux amd64 任务镜像。原生 Linux Docker 尚未验证；runner 依赖 `host.docker.internal` 能访问宿主机。

需要 Docker Desktop 正在运行，以及 Git、Python、[uv](https://docs.astral.sh/uv/)。SDK 源码须包含 `src/agent_runtime`、`pyproject.toml`、`uv.lock`。需要能访问所选模型的 OpenAI API 连接；模型 API 会产生费用。

### 1. 克隆与安装

```sh
git clone https://github.com/silvermooooon/evaluate-harness.git
cd evaluate-harness
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 替换成你自己的兼容 SDK 仓库路径。
.venv/bin/python scripts/setup_local.py --sdk-path ../agent-runtime
.venv/bin/python scripts/setup_local.py --sdk-path ../agent-runtime --check
```

安装脚本冻结 SDK 源码、导出锁定依赖、准备 Linux Python 依赖与 OpenCode 二进制，并准备官方镜像和本地实验清单。首次下载可能较大；SDK 的 `.env`、会话和虚拟环境不会复制。安装产物都在 Git 忽略目录中；已有实验快照不会被静默覆盖。

配置来自 [config/task-selection.json](config/task-selection.json)，首次安装复制到 `reports/task-selection.json`。修改本地清单可以选择任务、重复次数和运行标签。当前只实现下列 ownCloud 文件任务，不应直接添加依赖其他服务的 TAC 任务。

### 2. 启动基准服务

在项目根目录运行，并保持 controller 终端开启：

```sh
docker compose -f infra/compose.yml -p tac-eval up -d
.venv/bin/python infra/controller.py
```

ownCloud 使用端口 `8092`，controller 使用 `2999`。基准账户 `theagentcompany / theagentcompany` 是上游公开的测试账户。更多部署细节见 [infra/README.md](infra/README.md)。

### 3. 启动模型预算代理

另开终端。可直接使用现有 SDK 的环境文件，也可根据 [.env.example](.env.example) 创建被 Git 忽略的本地 `.env`。

```sh
.venv/bin/python scripts/budget_proxy.py \
  --env-file /absolute/path/to/your/.env \
  --ledger runs/api-usage.jsonl \
  --budget 20
```

真实密钥只由宿主机代理读取，任务容器拿到占位凭据。默认复用环境文件里的 `HTTPS_PROXY`；添加 `--direct` 只让本次代理进程绕过该 HTTP 代理，不修改全局网络设置。一次连接探测成功不保证长任务期间连接持续可用。

这是可信本机评测环境：代理和 controller 没有面向公网的身份验证，不应开放到公网。它们需要容器能访问宿主机监听端口。

### 4. 运行与汇总

第三个终端运行：

```sh
# 四题 × 每题两次 × 两个 Harness，默认串行执行。
.venv/bin/python scripts/run_suite.py

.venv/bin/python scripts/summarize_results.py
.venv/bin/python scripts/audit_runs.py
```

只运行一个新尝试：

```sh
.venv/bin/python scripts/run_trial.py \
  --task ds-format-excel-sheets --harness opencode \
  --repeat 1 --label smoke-local
```

主汇总只纳入本地任务清单声明的标签、任务和重复次数；单独的 `smoke-local` 运行仍保留在其他分组。新一轮实验请先修改 `reports/task-selection.json` 的 `formal_run_label`，再运行 suite。已有完成且评分成功的运行会跳过；未完成记录需先检查，输出不会静默覆盖。

**不要并行启动多个 suite 或 trial。** 当前任务共用 ownCloud，每次执行都会重置它。代理续算同一账本的累计预算，`--budget 20` 不是每次重启再增加 20 美元。

### 5. 查看结果与停止服务

| 本地文件 | 内容 |
| --- | --- |
| `reports/results.json` / `results.csv` | 分组结果、逐次指标、配对差值与用量覆盖率 |
| `reports/integrity-audit.json` | 输入及适配器哈希、工具参数中的评分材料访问线索 |
| `reports/sdk-version.json` | SDK 快照身份与逐文件哈希 |
| `runs/<run-id>/result.json` | 原始评分、镜像 digest、执行状态、耗时 |
| `runs/<run-id>/agent/` | 原生配置、事件、工具结果和摘要 |
| `runs/<run-id>/workspace/` | 归档任务文件 |
| `runs/api-usage.jsonl` | 请求尝试、provider usage、预算扣账；不含 Authorization header |

审计只检查已记录的字面工具参数，未命中不等于强制阻止了评分材料访问。原镜像包含评估辅助文件；模型通过共同指令被禁止读取这些材料。

结束两个服务终端，然后停止本项目容器：

```sh
docker compose -f infra/compose.yml -p tac-eval stop
```

## 实验边界

| 条件 | 当前设置 |
| --- | --- |
| 模型 | `gpt-6-luna`，OpenAI Responses，medium reasoning |
| Harness | OpenCode v2.2.7；用户提供的 Agent Runtime SDK 冻结快照 |
| 每次限制 | 60 次上游请求尝试、900 秒、每响应 8192 输出 token |
| 预算 | 全账本默认 $20；每次运行保守扣账上限 $1.50 |
| 请求体 | 500,000 字节上限，属于评测约束，不是模型窗口 |
| 容器 | Linux amd64、2 CPU、3 GB 内存 |
| 当前任务 | PDF 裁页、Excel 格式、费用分析、94 份简历分类 |

每次运行使用新容器与会话、重置数据、保存同一输入的哈希。suite 按任务与重复次数交替两边执行顺序。Harness 保留各自工具和上下文实现，系统提示与工具集合不完全相同；这比较的是两种装配的整体行为，不能归因于单个循环机制。

Token 费用按 [pricing.py](scripts/pricing.py) 中标注日期的公开价格估算，含缓存读取/写入与长上下文费率，不是账单。缺失 usage 不按零费用处理：报告标明覆盖率，预算保留预留额。当前代理使用每百万输入 $0.50、输出 $1.00 的保守预算上界。历史实验使用的计费实现不同，公开报告已说明。

尚未覆盖完整 175 题、多服务/NPC、GUI、小时级任务、跨会话恢复或整个 Duet 产品。模型和 API 暂时固定；更换模型需要同步适配器、代理允许列表和计费配置，不能只改一个命令行参数。

## 项目结构

```text
adapters/   两种 Harness 的原生接入
config/     可公开的默认任务清单
infra/      ownCloud 部署与 reset/health 控制器
scripts/    安装、执行、预算、汇总和审计
docs/       公开实验摘要及聚合指标
```

`vendor/`、`.tools/`、`runs/`、`reports/`、`.env` 均留在本地。公开聚合数据不包含原始简历、交易表、模型请求全文或本机路径；因此它能复算公开数值，不能替代完整原始轨迹复核。上游来源与许可证说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
