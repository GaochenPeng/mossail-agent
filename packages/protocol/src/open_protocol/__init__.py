"""跨服务契约；不依赖 ORM 或 Agent SDK。"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_serializer, model_validator

TaskStatus = Literal['submitted', 'running', 'complete', 'failed', 'abandon']
TerminalStatus = Literal['complete', 'failed', 'abandon']


class ChatMessage(BaseModel):
    role: Literal['user', 'assistant']
    content: str


class ModelConfig(BaseModel):
    id: str | None = None


class AgentParams(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)


class AgentInput(BaseModel):
    thread_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    user_id: str = 'local-user'
    agent: str = 'assistant'
    user_prompt: str = Field(min_length=1, max_length=50000)
    history_messages: list[ChatMessage] = Field(default_factory=list)
    history_files: list[list[Any]] = Field(default_factory=list)
    params: AgentParams = Field(default_factory=AgentParams)
    is_hitl: bool = False


class ChatCommand(BaseModel):
    model_config = {'extra': 'forbid'}
    question: str = Field(min_length=1, max_length=50000)
    agent: str = 'assistant'
    model: ModelConfig = Field(default_factory=ModelConfig)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class MessageItem(BaseModel):
    index: int
    sender: Literal['user', 'assistant', 'tool'] = 'assistant'
    type: str
    status: Literal['loading', 'success', 'error']
    meta_data: dict[str, Any] = Field(default_factory=dict)


class PatchOperation(BaseModel):
    op: Literal['replace', 'add', 'remove', 'test', 'copy', 'move']
    path: str
    value: Any = None
    from_: str | None = Field(default=None, alias='from')

    @model_serializer(mode='wrap')
    def serialize(self, handler):
        result = handler(self)
        # JSON Patch 的 null 是有效值，不能被外层 exclude_none 丢弃。
        if self.op in {'add', 'replace', 'test'} and self.value is None:
            result['value'] = None
        return result


class Envelope(BaseModel):
    thread_id: str
    task_id: str
    msg_id: str
    op: Literal['add', 'replace', 'patch']
    task_status: TaskStatus = 'running'
    value: MessageItem | None = None
    patches: list[PatchOperation] | None = None

    @model_validator(mode='after')
    def check_operation(self) -> Envelope:
        if self.op in {'add', 'replace'} and self.value is None:
            raise ValueError('add/replace requires value')
        if self.op == 'patch' and self.patches is None:
            raise ValueError('patch requires patches')
        return self


class StreamCursor(BaseModel):
    task_id: str = Field(min_length=1)
    seq: int = Field(ge=1)


class MessageUpdate(BaseModel):
    protocol_version: Literal[2] = 2
    event_v2: Envelope
    stream: StreamCursor | None = None
