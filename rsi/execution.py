"""代码 RSI 执行边界；PG/Redis 隔离评测器尚未接入。"""


class ExecutionUnavailable(RuntimeError):
    """执行条件尚未具备，不产生通过结果或真实模型调用。"""


def require_isolated_execution():
    raise ExecutionUnavailable(
        '代码 RSI 隔离执行尚未适配单 Server＋PG/Redis；旧 SQLite Backend 已移除。'
        '请先实现并验收独立 PG schema、Redis namespace 与候选进程隔离，'
        '再启用 evaluate/run；不会连接开发数据或回退旧服务。'
    )
