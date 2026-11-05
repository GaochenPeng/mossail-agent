# 本机首版验收

更新：2026-11-05。当前入口是8010上的单个薄Agent Server，使用PostgreSQL和Redis。运行命令、模块职责、真实时序与失败边界见[本机首版说明](acceptance/mossail-local-v1.md)。

当前目录按MVC＋Service/Repository组织。进程epoch与明确中断后的检查点回退已纳入工程检查和真实异常退出验收；同epoch继续拒绝水位回退。以下结论限本机单进程版本。

| 验证 | 结果与范围 |
| --- | --- |
| Agent回归、Mossail记录/隔离PGRedis联调、前端状态与组件检查 | Python 87项通过（59 Agent＋28 Mossail），前端28项通过 |
| 本机启动/停止/重复启动、静态资源、REST线程、WS同步/ping | 通过 |
| 真实HTTP两轮计算及工具 | 两轮complete，答案/工具结果分别为126/126.0、130/130.0 |
| 重复提交、运行中WS断开/重连、同会话第二轮上下文 | 真实HTTP/WS检查通过 |
| 浏览器DeepSearch完成与刷新 | 最终版PEP 654为complete、seq9，2次工具调用成功、1条引用；完成后刷新消息区逐字一致，运行中刷新亦通过 |
| 浏览器停止 | 模型等待且0工具时取消，abandon、seq3；PG与GET一致，Redis镜像删除，刷新仍显示已停止 |
| 真实进程异常退出与重启 | SIGKILL后Redis seq3恢复PG failed/INTERRUPTED seq4；epoch切换、浏览器显示及不自动重跑通过 |
| Docker应用容器、多进程、多SDK、DAG及新架构RSI联调 | 未验收 |

原始请求、帧、运行指纹和历史原型记录保留在验收机器，不随公开源码发布。旧Backend/SQLite原型与历史实验的通过结果不作为此版PG/Redis的新验证证据。

检查点恢复不保证保留页面最后每个字符。Redis的1秒门槛由新帧到达触发检查，没有独立定时flush，也不构成最大数据损失窗口。

运行服务的`health.agent.execution.source_sha256`为`92b5bb13fa2dd7e090bbde31e43902f213727f1b10a58a3a1607eb4812c361a9`。检索和正文读取依赖外部网络，验收期间的网络故障已恢复。停止证据覆盖模型等待且未执行工具的阶段，外部工具取消或已发生副作用的撤销需逐项验证。上述固定场景不代表开放域回答质量、容量或生产部署均已通过。
