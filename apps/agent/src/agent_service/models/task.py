"""任务记录和接纳结果：只有数据，不持有连接或执行协程。"""
from dataclasses import dataclass, field
from typing import Any

from open_protocol import ChatCommand

TERMINAL = {'complete', 'failed', 'abandon'}


@dataclass
class TaskRecord:
    id: str
    thread_id: str
    command: ChatCommand
    status: str = 'submitted'
    error: str | None = None
    generation: int = 0
    seq: int = 0
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: str = ''
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'thread_id': self.thread_id,
            'command': self.command.model_dump(), 'status': self.status,
            'error': self.error, 'generation': self.generation, 'seq': self.seq,
            'snapshots': self.snapshots, 'created_at': self.created_at,
            'finished_at': self.finished_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> 'TaskRecord':
        return cls(
            id=value['id'], thread_id=value['thread_id'],
            command=ChatCommand.model_validate(value['command']),
            status=value.get('status', 'submitted'), error=value.get('error'),
            generation=int(value.get('generation', 0)), seq=int(value.get('seq', 0)),
            snapshots=value.get('snapshots', {}), created_at=value.get('created_at', ''),
            finished_at=value.get('finished_at'),
        )


@dataclass
class TaskAdmission:
    task: TaskRecord
    created: bool
