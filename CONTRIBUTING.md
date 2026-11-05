# 参与开发

Mossail当前发布范围是薄Agent Server、PG/Redis适配、Agent Harness、协议和Web前端。本机运行方式见[README](README.md)，消息链路与验证范围见[首版说明](docs/acceptance/mossail-local-v1.md)。

## 代码风格

- 内部运行数据优先使用普通`dataclass`和具名结果；网络协议继续使用已有协议对象。让名称直接表达输入、状态和输出。
- 使用小函数、单一目的与早返回，减少嵌套。说明和代码注释优先中文，沿用必要的技术术语。
- 通过构造参数和普通对象组合模块；接口可使用duck typing。只有确实需要静态约束或多个实现共用契约时才增加`Protocol`。
- 保持直接的函数调用与资源生命周期。不要为了分层引入继承树、通用基类、重型DDD模板或每层一份重复的数据对象。

## 模块边界

采用MVC与Service/Repository辅助层，具体目录承担具体职责；不把所有实现压进三个文件。

| 位置 | 应承担的工作 |
| --- | --- |
| `main.py` | 组合入口：创建对象、注入依赖、注册Controller与View、管理启动/关闭顺序 |
| `controllers/http.py`、`controllers/websocket.py` | Controller：协议解析、响应与连接生命周期；业务命令/查询调用Service |
| `models/task.py`、`models/message.py` | Model：TaskRecord、TaskAdmission等dataclass与纯消息归并规则，描述身份、状态和完整消息快照；不持有I/O资源 |
| `services/tasks.py`：`TaskService` | 接纳与校验请求、读取上下文、持有执行协程、调用Harness、超时/停止和调用终态保存；消息归并交给RecordService |
| `services/records.py`：`RecordService` | 消息归并、任务seq、会话记录锁、PG/Redis操作顺序、完整同步与待保存终态；通过Repository访问中间件 |
| `repositories/postgres.py`：`PgStore` | PG连接、SQL与事务、会话/任务/完整消息投影、重复请求约束、代次与终态保护 |
| `repositories/redis.py`：`RedisBus` | Redis连接、运行镜像TTL与代次/水位检查、Pub/Sub订阅和有界队列清理；执行协程由TaskService管理 |
| `agents/registry.py`、`agents/model_access.py`、`agents/chatgpt_auth.py` | 场景、模型客户端与认证资源；返回AgentRegistry给组合入口，不访问FastAPI app.state |
| `errors.py` | 跨层共用的明确异常，避免为了异常类型产生层间反向依赖 |
| `apps/web` | View：浏览器独立消息副本、用户交互与按type渲染的组件 |

主业务调用方向为`View → Controller → Service → Repository`，Service/Repository使用Model，Model不反向调用这些模块。HTTP健康检查调用`TaskService.health()`，不在Controller探测或读写PG/Redis；WS Controller只通过注入的RedisBus管理订阅队列，不处理持久化、运行镜像或任务执行。`agents/`负责模型供应商和场景资源，它不是MVC的Model层。

Service通过构造参数取得store、bus和Runtime工厂，不导入具体Repository类。Repository可以依赖Model、协议、跨层异常和驱动，不反向导入Service/Controller；`errors.py`不反向导入其他层。

[架构依赖检查](tests/mossail/test_architecture.py)约束主要反向导入。这是静态边界检查，消息时序、资源清理和执行行为仍需对应回归验证。

Harness负责模型/工具循环，浏览器维护自己的状态副本。修改边界时说明输入、输出、等待顺序和失败后是否继续；PG提交、Redis发布、浏览器收到与页面应用是不同完成条件。目录迁移后重新核对导入与路由；旧平铺模块不保留兼容壳。

保留恢复规则的两个边界：同`server_epoch`拒绝同步水位回退；新socket首个有效sync确认进程epoch变化后，只允许对`failed/INTERRUPTED`任务采用较早检查点，并保留旧socket隔离。检查点未包含的文本可能丢失；Redis的1秒节流只在新帧到达时检查，不能表述为定时持久化或最大损失窗口。

DeepSearch可规范化来源ID的引用括号，但仍须校验来源已登记且正文已读取，最终链接由程序生成；未登记/未读取的引用或模型直接生成的URL应拒绝并返回`failed`。验证停止行为时，记录取消发生的阶段和已执行工具；模型等待阶段的取消证据不覆盖所有工具的取消或外部副作用撤销。

## 变更与验证

先查看当前代码和契约，说明要解决的具体行为。新的架构建议、阈值或扩展设计应标为建议或草案，未经确认不要写成已批准的项目约定。

按改动范围运行相关检查；新增回归应覆盖具体故障或行为边界。区分纯函数/替身测试、真实PG/Redis联调、真实模型调用和浏览器验收。只通过健康检查或静态分析时，保留这个验证范围，不推断为生产可用或整体质量提升。

保持本机首版单Server的运行边界。需要新部署方式、后台执行分派或执行恢复时，先说明设计和验收条件，再实施对应变化。

## 提交范围

不要提交`.env`、`.env.local`、OAuth凭证、访问令牌、`.data`运行数据、内部参考或个人配置；`.env.example`只保留配置名称和占位值。提交前查看变更清单和内容。

本地旧工作区可能保留Backend、SQLite、RSI实验和教学档案；不要把这些历史依赖引入当前发布路径。原始验收记录可留在本机，公开说明只包含可核对的工程入口、技术机制、验证范围与限制。
