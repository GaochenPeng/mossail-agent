// 浏览器消息副本：只有完成校验的同步或连续增量才能推进水位。
const forbidden = new Set(['__proto__', 'prototype', 'constructor']);
const terminal = new Set(['complete', 'failed', 'abandon']);
const statuses = new Set(['submitted', 'running', ...terminal]);
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const clone = value => structuredClone(value);
const messageKey = (taskId, msgId) => JSON.stringify([taskId, msgId]);

function pointer(path) {
  if (typeof path !== 'string' || (path !== '' && !path.startsWith('/'))) throw new Error('无效 JSON Pointer');
  if (path === '') return [];
  return path.slice(1).split('/').map(part => {
    if (/~(?![01])/u.test(part)) throw new Error('无效 JSON Pointer 转义');
    const key = part.replace(/~1/g, '/').replace(/~0/g, '~');
    if (forbidden.has(key)) throw new Error('禁止访问原型路径');
    return key;
  });
}

function arrayIndex(target, key, allowEnd = false) {
  if (key === '-' && allowEnd) return target.length;
  if (!/^(0|[1-9]\d*)$/u.test(key)) throw new Error('无效数组下标');
  const index = Number(key);
  if (!Number.isSafeInteger(index) || index > target.length || (!allowEnd && index === target.length)) {
    throw new Error('数组下标越界');
  }
  return index;
}

function read(document, path) {
  let value = document;
  for (const key of path) {
    if (Array.isArray(value)) value = value[arrayIndex(value, key)];
    else if (object(value) && own(value, key)) value = value[key];
    else throw new Error('JSON Patch 路径不存在');
  }
  if (value === undefined) throw new Error('JSON Patch 路径不存在');
  return value;
}

function write(document, path, operation, value) {
  if (!path.length) {
    if (operation !== 'add' && document === undefined) throw new Error('JSON Patch 根不存在');
    return operation === 'remove' ? undefined : clone(value);
  }
  const parent = read(document, path.slice(0, -1));
  const key = path.at(-1);
  if (Array.isArray(parent)) {
    const index = arrayIndex(parent, key, operation === 'add');
    if (operation === 'add') parent.splice(index, 0, clone(value));
    else if (operation === 'remove') parent.splice(index, 1);
    else parent[index] = clone(value);
  } else if (object(parent)) {
    if (operation !== 'add' && !own(parent, key)) throw new Error('JSON Patch 路径不存在');
    if (operation === 'remove') delete parent[key];
    else parent[key] = clone(value);
  } else throw new Error('JSON Patch 父节点不是对象');
  return document;
}

function equal(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => equal(value, right[index]));
  }
  if (object(left) && object(right)) {
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every(key => own(right, key) && equal(left[key], right[key]));
  }
  return false;
}

export function applyPatch(document, operations) {
  if (!Array.isArray(operations)) throw new Error('JSON Patch 必须是数组');
  let result = clone(document);
  for (const operation of operations) {
    if (!object(operation)) throw new Error('无效 JSON Patch 操作');
    const {op} = operation;
    const path = pointer(operation.path);
    if (['add', 'replace', 'test'].includes(op) && !own(operation, 'value')) throw new Error('JSON Patch 缺少 value');
    if (op === 'test') {
      if (!equal(read(result, path), operation.value)) throw new Error('JSON Patch test 失败');
    } else if (['add', 'remove', 'replace'].includes(op)) {
      result = write(result, path, op, operation.value);
    } else if (op === 'copy' || op === 'move') {
      const from = pointer(operation.from);
      const value = clone(read(result, from));
      if (op === 'move') {
        if (from.length < path.length && from.every((key, index) => key === path[index])) throw new Error('不能移动到自己的子节点');
        result = write(result, from, 'remove');
      }
      result = write(result, path, 'add', value);
    } else throw new Error('不支持的 JSON Patch 操作：' + op);
  }
  return result;
}

function validateMessage(value) {
  if (!object(value) || !Number.isInteger(value.index) || typeof value.type !== 'string'
    || !['user', 'assistant', 'tool'].includes(value.sender)
    || !['loading', 'success', 'error'].includes(value.status) || !object(value.meta_data)) {
    throw new Error('无效消息快照');
  }
  return clone(value);
}

export class StreamState {
  constructor(threadId) {
    this.threadId = threadId;
    this.items = new Map();
    this.heads = new Map();
    this.tasks = new Map();
    this.synced = false;
    this.serverEpoch = null;
    this.connectionEpoch = null;
  }

  beginConnection() {
    this.connectionEpoch = null;
    this.synced = false;
  }

  get activeTask() {
    return [...this.tasks.values()].reverse().find(task => !terminal.has(task.status));
  }

  get latestTask() { return [...this.tasks.values()].at(-1); }

  get orderedItems() {
    const taskOrder = new Map([...this.tasks.keys()].map((id, index) => [id, index]));
    return [...this.items].sort(([leftKey, left], [rightKey, right]) =>
      taskOrder.get(JSON.parse(leftKey)[0]) - taskOrder.get(JSON.parse(rightKey)[0]) || left.index - right.index);
  }

  acceptTask(frame) {
    if (frame.thread_id && frame.thread_id !== this.threadId) return false;
    if (typeof frame.task_id !== 'string' || !frame.task_id) return false;
    const previous = this.tasks.get(frame.task_id);
    // 接纳回复可能晚于终态更新，不能使完成的任务再次运行。
    if (!previous || !terminal.has(previous.status)) {
      this.tasks.set(frame.task_id, {...previous, ...frame, status: statuses.has(frame.status) ? frame.status : 'submitted'});
    }
    return true;
  }

  consume(frame) {
    if (frame.type === 'sync') return this.sync(frame);
    if (frame.type === 'heads') return this.compareHeads(frame);
    const event = frame.event_v2;
    if (!event || event.thread_id !== this.threadId) return {kind: 'ignore'};
    try {
      const stream = frame.stream;
      if (frame.protocol_version !== 2 || !this.synced || !stream || stream.task_id !== event.task_id
        || typeof event.task_id !== 'string' || !event.task_id || !Number.isSafeInteger(stream.seq) || stream.seq < 1) {
        throw new Error('消息缺少同步基线或水位');
      }
      const head = this.heads.get(event.task_id) ?? 0;
      if (stream.seq <= head) return {kind: 'ignore'};
      if (stream.seq !== head + 1) throw new Error('消息序号缺口');
      if (!statuses.has(event.task_status) || typeof event.msg_id !== 'string' || !event.msg_id) throw new Error('无效消息身份或任务状态');
      const previousTask = this.tasks.get(event.task_id);
      if (terminal.has(previousTask?.status) && previousTask.status !== event.task_status) throw new Error('终态不可回退');
      const key = messageKey(event.task_id, event.msg_id);
      let next;
      if (event.op === 'add' || event.op === 'replace') {
        if (event.op === 'add' && this.items.has(key)) throw new Error('不能重复 add 同一消息');
        next = validateMessage(event.value);
      }
      else if (event.op === 'patch') {
        if (!this.items.has(key)) throw new Error('patch 缺少消息基线');
        next = validateMessage(applyPatch(this.items.get(key), event.patches));
      } else throw new Error('无效消息操作');
      this.items.set(key, next);
      this.heads.set(event.task_id, stream.seq);
      this.tasks.set(event.task_id, {...previousTask, task_id: event.task_id, status: event.task_status, seq: stream.seq});
      return {kind: 'update', key};
    } catch (error) {
      return {kind: 'resync', reason: error.message};
    }
  }

  compareHeads(frame) {
    if (frame.thread_id !== this.threadId) return {kind: 'ignore'};
    if (!this.synced || frame.server_epoch !== this.serverEpoch || !object(frame.heads) || !Array.isArray(frame.tasks)) return {kind: 'resync'};
    for (const [id, seq] of Object.entries(frame.heads)) {
      if (!Number.isSafeInteger(seq) || seq < 0 || !this.heads.has(id) || this.heads.get(id) < seq) return {kind: 'resync'};
    }
    for (const task of frame.tasks) {
      if (!object(task) || !own(frame.heads, task.task_id) || task.seq !== frame.heads[task.task_id] || !statuses.has(task.status)) {
        return {kind: 'resync'};
      }
      const local = this.tasks.get(task.task_id);
      if (!local || (task.seq === local.seq && (
        (task.status !== local.status && !terminal.has(local.status))
        || Boolean(task.save_pending) !== Boolean(local.save_pending)
      ))) return {kind: 'resync'};
    }
    return {kind: 'ignore'};
  }

  sync(frame) {
    if (frame.thread_id !== this.threadId) return {kind: 'ignore'};
    try {
      const epoch = frame.server_epoch;
      if (typeof epoch !== 'string' || !epoch || (this.connectionEpoch !== null && this.connectionEpoch !== epoch)) {
        throw new Error('服务实例变化需要新连接确认');
      }
      const changedEpoch = this.serverEpoch !== null && this.serverEpoch !== epoch;
      if (!object(frame.heads) || !Array.isArray(frame.tasks) || !Array.isArray(frame.messages)) throw new Error('无效同步帧');
      const heads = new Map(Object.entries(frame.heads));
      if ([...heads].some(([id, seq]) => !id || !Number.isSafeInteger(seq) || seq < 0)) throw new Error('无效同步水位');
      const tasks = new Map();
      for (const task of frame.tasks) {
        if (!object(task) || typeof task.task_id !== 'string' || !heads.has(task.task_id)
          || tasks.has(task.task_id) || !statuses.has(task.status) || task.seq !== heads.get(task.task_id)) {
          throw new Error('无效同步任务');
        }
        const previous = this.tasks.get(task.task_id);
        if (terminal.has(previous?.status) && previous.status !== task.status) throw new Error('同步终态回退');
        tasks.set(task.task_id, clone(task));
      }
      if (heads.size !== tasks.size) throw new Error('同步任务水位不完整');
      let recoveredCheckpoint = false;
      for (const [id, seq] of this.heads) {
        if ((heads.get(id) ?? -1) >= seq) continue;
        const recovered = tasks.get(id);
        // 新进程只能用明确中断的检查点替换丢失的内存更新；同进程绝不回退。
        if (!changedEpoch || !recovered || recovered.status !== 'failed'
          || typeof recovered.error !== 'string' || !recovered.error.includes('INTERRUPTED')) {
          throw new Error('同步水位回退');
        }
        recoveredCheckpoint = true;
      }
      const items = new Map();
      for (const message of frame.messages) {
        if (!object(message) || !tasks.has(message.task_id) || typeof message.msg_id !== 'string' || !message.msg_id) throw new Error('无效同步消息身份');
        const key = messageKey(message.task_id, message.msg_id);
        if (items.has(key)) throw new Error('重复同步消息');
        items.set(key, validateMessage(message.value));
      }
      this.items = items;
      this.heads = heads;
      this.tasks = tasks;
      this.synced = true;
      this.serverEpoch = epoch;
      this.connectionEpoch = epoch;
      return {kind: 'sync', recoveredCheckpoint};
    } catch (error) {
      return {kind: 'resync', reason: error.message};
    }
  }
}
