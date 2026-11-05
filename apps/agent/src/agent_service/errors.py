"""用例与适配器的可公开错误；不携带供应商响应和凭证。"""


class StorageError(RuntimeError):
    """存储不可用，调用方可重试记录，但不能据此重跑任务。"""


class InvalidCommand(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code
