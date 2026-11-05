# Mossail Agent

[GitHub](https://github.com/GaochenPeng/mossail-agent) · MIT

轻量的全栈Agent应用框架。当前本机首版由一个薄Agent Server、PostgreSQL、Redis和同仓前端组成，提供计算助手与DeepSearch场景，复用既有Agent Harness与工具循环。

## 运行本机首版

准备Python 3.12及以上、uv、可连接的PostgreSQL和Redis。参考[.env.example](.env.example)创建本机`.env.local`，填写模型认证、模型名、中间件连接，以及DeepSearch所需的Serper配置；已有私人配置继续保留。PG应用账号需要在目标库中创建schema/table的权限，使用非superuser账号。

本机验证使用PG `127.0.0.1:15432`、Redis `127.0.0.1:16379`。中间件实例、凭证与运行数据不随源码发布，首次运行需先自行准备中间件。

```bash
uv sync --project apps/agent --frozen
python3 scripts/dev.py --skip-sync --port 8010 --background
```

打开[本机工作台](http://127.0.0.1:8010)。前端、HTTP和`/ws/chat/`共用8010端口。去掉`--background`可在前台运行，前台使用Ctrl+C停止。查看和停止后台Server：

```bash
python3 scripts/dev.py --status
python3 scripts/dev.py --stop
```

重复启动会复用本脚本记录的Server；停止前核对PID、启动时间和命令。端口被其他进程占用时使用`--port`选择其他端口。后台日志保存在`.data/runtime/mossail-agent.log`。

```bash
curl -fsS http://127.0.0.1:8010/health
```

健康检查应包含`service=mossail`、`ready=true`、`storage.postgres=true`和`storage.redis=true`；`agent.agents`列出可用场景。PG与Redis为必选，缺少配置或连接失败时不回退SQLite。健康检查只能验证当前装配状态，真实任务与页面结果见[本机验收说明](docs/acceptance/mossail-local-v1.md)。

## MVC与辅助层

前端承担View，HTTP/WebSocket入口承担Controller，任务数据对象承担Model。业务流程放在Service，中间件访问放在Repository；Agent场景与认证单独装配。这是MVC加Service/Repository辅助层，目录职责如下：

```text
apps/agent/src/agent_service/
├── main.py                   # 组合入口
├── errors.py                 # 跨层异常
├── controllers/
│   ├── http.py
│   └── websocket.py
├── models/
│   ├── task.py               # 任务数据对象
│   └── message.py            # 不含I/O的消息归并规则
├── services/
│   ├── tasks.py
│   └── records.py
├── repositories/
│   ├── postgres.py
│   └── redis.py
└── agents/
    ├── registry.py
    ├── model_access.py
    └── chatgpt_auth.py
apps/web/                     # View：页面、状态与组件
```

| 模块 | 职责 |
| --- | --- |
| [main.py](apps/agent/src/agent_service/main.py) | 组合模型工厂、Repository、Service与Controller，管理资源启动/关闭 |
| Controller：[HTTP](apps/agent/src/agent_service/controllers/http.py)、[WebSocket](apps/agent/src/agent_service/controllers/websocket.py) | 解析命令和生成响应，管理连接、订阅与完整同步 |
| Model：[task.py](apps/agent/src/agent_service/models/task.py)、[message.py](apps/agent/src/agent_service/models/message.py) | TaskRecord、TaskAdmission等数据对象与纯消息归并规则；不持有数据库、socket或执行协程 |
| Service：[tasks.py](apps/agent/src/agent_service/services/tasks.py)、[records.py](apps/agent/src/agent_service/services/records.py) | 执行、停止、历史、消息归并及记录/发布顺序 |
| Repository：[postgres.py](apps/agent/src/agent_service/repositories/postgres.py)、[redis.py](apps/agent/src/agent_service/repositories/redis.py) | PG事务/持久化，Redis运行镜像、代次检查与Pub/Sub分发 |
| [Agent装配](apps/agent/src/agent_service/agents/registry.py) | 返回普通AgentRegistry对象，保存场景工厂、模型身份与资源；由main向任务服务注入，不依赖FastAPI app.state |
| [Agent Harness](packages/agent-runtime/src/agent_runtime/runtime.py) | 模型工具循环，将流事件转换为消息更新；场景工具与服务存储分开 |
| [协议](packages/protocol/src/open_protocol/__init__.py)与View：[前端](apps/web/index.html) | thread/task/msg身份、带seq的增量和完整sync；浏览器检查水位并按type渲染组件 |

主业务调用方向是`View → Controller → Service → Repository`；Service和Repository使用Model。Service通过注入对象调用仓储，不导入具体仓储类。`main.py`完成对象组合，MVC的Model与`agents/`中的模型供应商适配是不同概念。阅读顺序为`main → controllers → services/tasks → services/records → models/message → repositories → apps/web`；输入、处理、依赖图与失败边界见[技术链路](docs/acceptance/mossail-local-v1.md#技术链路)。

首版固定一个Server进程、一个uvicorn Worker。PG会话锁拒绝同一schema的第二个Server，同一会话只接纳一个活动任务。Server异常退出后，旧任务会在下次启动时标为`failed/INTERRUPTED`，需要重新提交，不会续跑Agent循环。

刷新或重连恢复消息快照。进程重启可能只能找回较早的保存检查点：新socket的首个有效sync通过`server_epoch`确认新服务实例，并仅对明确中断的任务接受检查点水位回退；同epoch仍拒绝回退，旧socket帧隔离。界面会说明检查点恢复，最后尚未保存的文本可能丢失。Redis的1秒节流门槛只在新帧到达时检查，不是定时持久化或最大数据损失窗口。

## 当前验证范围

当前本机版通过87项Python检查（59项Agent、28项Mossail）和28项前端检查。真实HTTP两轮计算完成，答案与工具结果为126/126.0、130/130.0；同会话上下文、重复提交去重、断开WebSocket后继续执行及历史读取通过。

DeepSearch运行中刷新通过；最终版PEP 654任务达到`complete`，2次工具调用成功、1条引用，完成后刷新消息区逐字一致。停止用例在模型等待、尚无工具调用时取消，任务保存为`abandon`，Redis运行镜像删除，刷新仍显示已停止；其他工具的取消能力与已有外部副作用的撤销需要逐项验证。

真实SIGKILL检查确认：旧任务从Redis检查点恢复为PG中的`failed/INTERRUPTED`，服务epoch切换，浏览器显示中断结果，Agent没有自动重跑。检索和正文读取依赖外部网络，验收期间的网络故障已恢复。范围与运行指纹见[验收入口](docs/acceptance.md)。

```bash
apps/agent/.venv/bin/python -m pytest tests/agent tests/mossail -q
node --test tests/web/*.test.mjs
```

前端工程检查在Node 22环境验证。

当前未验收应用容器、多Worker、跨实例执行恢复、DAG、第二SDK与复杂HITL。旧SQLite Backend、双服务容器配置及旧验收入口已移除；当前只使用上述薄Agent Server启动路径。

代码RSI保留案例材料校验、实验冻结、本地候选和维护者评审；`evaluate/run`在读取模型配置或启动服务之前明确拒绝执行。独立PG schema、Redis namespace及候选进程隔离尚待适配，不复用开发数据或将历史双服务结果当作当前验收，见[RSI状态](rsi/README.md)。

## 本地资料与发布范围

本次发布包含Agent服务、Web前端、Harness/协议、对应测试、启动脚本、本机说明，以及案例/RSI材料冻结与评审工具；RSI隔离执行仍未适配。本地工作区还保留历史实验、教学材料、内部参考和原始验收记录，它们不属于本次公开发布内容。私人配置、认证凭证和运行数据不入库。

开发风格、模块边界和提交范围见[CONTRIBUTING](CONTRIBUTING.md)。
