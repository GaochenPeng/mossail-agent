# 代码 RSI：材料冻结、本地候选与维护者评审

本目录保存代码改进实验的材料和评审流程。当前在线架构为薄 Agent Server＋PostgreSQL＋Redis；隔离执行器尚未适配，不能把这里的准备能力表述为自主 RSI 已完成。

## 当前边界

| 环节 | 当前状态 |
| --- | --- |
| 导入已评审失败案例，校验原始材料和独立要求 | 可用，基于案例文件，不自动诊断 |
| 冻结计划、源码、候选范围与验收材料 | 可用 |
| 导入当前 Codex 或开发者准备的本地 proposal | 可用，不自动调用模型生成源码 |
| 基线与候选隔离执行、真实任务比较 | 尚未适配，evaluate/run 明确拒绝执行 |
| 生成候选差异、独立 Git 评审包与人工决定记录 | 可用；证据不足时给出 inconclusive |
| 应用候选、合并和推送主项目 | 不自动执行，由维护者另行决定 |

旧 SQLite Backend 和双服务启动器已移除。evaluate/run 在读取模型配置、创建实验目录或启动服务之前报错；直接调用 Experiment.evaluate 也不能绕过这一限制。

恢复隔离执行前，需要验收独立 PG schema、Redis namespace、候选进程、文件与凭证边界，以及当前消息协议和运行指纹。禁止借用开发数据验证候选。保留的转发和 sandbox 组件不代表新执行器已经接入。

## 本地使用

在仓库根目录完成 Agent 依赖安装后，使用同一 Python 环境：

```bash
apps/agent/.venv/bin/python scripts/rsi.py prepare --plan rsi/plans/url-robustness.json
```

输出位于 .data/rsi/experiments 下。将下面的 EXPERIMENT 替换为该目录：

```bash
apps/agent/.venv/bin/python scripts/rsi.py propose --experiment EXPERIMENT --proposal LOCAL_PROPOSAL_JSON
apps/agent/.venv/bin/python scripts/rsi.py review --experiment EXPERIMENT
```

本地 proposal 至少包含 rationale 和 changes；changes 每项给出允许的 path 及完整 content。示例 URL 计划限定到 research.py 的 public_url 符号，保护签名和范围外文本。该示例缺陷在当前源码中已修复，不代表存在新的优化收益。

案例驱动的准备使用 prepare --case CASE_DIRECTORY --plan PLAN_FILE。案例目录必须包含 case.json、raw-tasks.json、criteria.json 和 suite.json；任务已结束、评价为 fail，且原始材料与独立要求的哈希一致。计划须明确 objective、planning.mode=local_manual 和 planning.producer。

绑定案例的 proposal 还须给出匹配的 source_case_id，以及 provenance.producer 和 provenance.external_source_request=false。原案例 suite 只作为冻结证据，不自动替代保留回归集或重放。

维护者决定使用：

```bash
apps/agent/.venv/bin/python scripts/rsi.py decision --experiment EXPERIMENT --decision reject --reviewer MAINTAINER --notes REVIEW_REASON
```

该命令只记录决定，不修改主项目源码。接受条件由冻结策略与执行证据约束；当前没有新执行证据时，不应宣称候选已通过真实任务比较。

## 模块

| 模块 | 职责 |
| --- | --- |
| workspace.py | 源码快照、清单、哈希与允许路径 |
| change_scope.py | 文件或 Python 符号粒度、签名与范围外文本检查 |
| case_input.py | 案例材料校验、来源绑定和候选请求 |
| experiment.py | 准备、本地候选、状态校验与维护者评审 |
| review.py | 独立实验 Git 仓库、差异和 bundle，不推送主仓库 |
| execution.py | 明确拒绝尚未适配的隔离执行 |
| isolation.py / model_gateway.py | 保留的隔离与转发组件，未装配为当前执行能力 |
| proposal.py | 历史外部生成适配器，当前 CLI 不调用 |

scripts/export_case.py、review_case.py 和 evaluate.py 中的材料转换及确定性检查由案例逻辑复用。在线导出和旧验收 CLI 尚未完成新完整同步格式的适配，不属于当前在线验收入口；当前服务验收使用 scripts/verify_mossail.py。

源码快照排除私人配置、运行数据、凭证、虚拟环境和 Git 元数据。报告中的历史 backend/agent 工程字段仅保留读取兼容，不代表当前启动两个服务。

## 验证

```bash
apps/agent/.venv/bin/python -m pytest tests/rsi tests/integration -q
```

这些检查覆盖材料、范围、评审与执行拒绝边界。转发集成测试使用模拟上游和本机 loopback，不调用真实模型，不证明优化收益或新隔离执行器完成。
