"""消息状态归并规则：在副本上校验并更新，不读写外部资源。"""
import copy
from dataclasses import replace

import jsonpatch
from open_protocol import MessageItem, MessageUpdate

from .task import TaskRecord


def apply_update(task: TaskRecord, update: MessageUpdate) -> TaskRecord:
    """先在副本应用；失败不会破坏最后一个有效快照。"""
    event = update.event_v2
    if (event.thread_id, event.task_id) != (task.thread_id, task.id):
        raise ValueError('消息地址与当前任务不一致；首版未启用跨任务交互')
    snapshots = copy.deepcopy(task.snapshots)
    if event.op == 'add':
        if event.msg_id in snapshots:
            raise ValueError('消息已存在，不能重复 add')
        snapshots[event.msg_id] = event.value.model_dump()
    elif event.op == 'replace':
        snapshots[event.msg_id] = event.value.model_dump()
    else:
        if event.msg_id not in snapshots:
            raise ValueError('patch 缺少消息基线')
        operations = [patch.model_dump(by_alias=True, exclude_unset=True) for patch in event.patches]
        snapshots[event.msg_id] = jsonpatch.apply_patch(snapshots[event.msg_id], operations, in_place=False)
    MessageItem.model_validate(snapshots[event.msg_id])
    return replace(task, snapshots=snapshots, seq=task.seq + 1)
