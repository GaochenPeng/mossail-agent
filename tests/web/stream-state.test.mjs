import test from 'node:test';
import assert from 'node:assert/strict';
import {applyPatch, StreamState} from '../../apps/web/stream-state.js';

const item = content => ({index: 1, sender: 'assistant', type: 'markdown', status: 'loading', meta_data: {content}});
const sync = (seq = 0, content, status = 'running') => ({
  type: 'sync', thread_id: 'thread', server_epoch: 'epoch-a', heads: {task: seq},
  tasks: [{task_id: 'task', status, error: null, seq, client_request_id: 'request'}],
  messages: content === undefined ? [] : [{task_id: 'task', msg_id: 'message', value: item(content)}],
});
const update = (seq, content, status = 'running', op = 'patch') => ({
  protocol_version: 2, stream: {task_id: 'task', seq}, event_v2: {
    thread_id: 'thread', task_id: 'task', msg_id: 'message', op, task_status: status,
    ...(op === 'patch' ? {patches: [{op: 'replace', path: '/meta_data/content', value: content}]} : {value: item(content)}),
  },
});
const first = state => [...state.items.values()][0];

test('完整支持 add/remove/replace/move/copy/test，保留 null 并不修改原对象', () => {
  const before = {a: [1, 2, 3], 'a/b': {'~key': false}, other: {kept: true}};
  const result = applyPatch(before, [
    {op: 'add', path: '/a/1', value: 8},
    {op: 'add', path: '/a/-', value: 4},
    {op: 'remove', path: '/a/0'},
    {op: 'move', from: '/a/0', path: '/a/2'},
    {op: 'copy', from: '/other', path: '/copied'},
    {op: 'replace', path: '/a~1b/~0key', value: null},
    {op: 'test', path: '/a', value: [2, 3, 8, 4]},
    {op: 'test', path: '/copied', value: {kept: true}},
  ]);
  assert.deepEqual(result.a, [2, 3, 8, 4]);
  assert.equal(result['a/b']['~key'], null);
  assert.deepEqual(before.a, [1, 2, 3]);
  result.copied.kept = false;
  assert.equal(result.other.kept, true);
  assert.equal(before.other.kept, true);
});

test('根替换、空键、对象 test 忽略字段顺序，移动到原位置有效', () => {
  assert.deepEqual(applyPatch({}, [{op: 'replace', path: '', value: {'': 1}}]), {'': 1});
  assert.deepEqual(applyPatch({'': 1}, [{op: 'move', from: '/', path: '/'}]), {'': 1});
  assert.deepEqual(applyPatch({a: [1, 2]}, [{op: 'move', from: '/a/0', path: '/a/0'}]), {a: [1, 2]});
  assert.deepEqual(applyPatch({a: 1, b: 2}, [{op: 'test', path: '', value: {b: 2, a: 1}}]), {a: 1, b: 2});
});

test('错误路径、越界、test失败、未知操作均抛错，补丁组原子失败', () => {
  const before = {a: [1], nested: {x: 1}};
  for (const operation of [
    {op: 'replace', path: '/missing', value: null},
    {op: 'remove', path: '/a/1'},
    {op: 'add', path: '/a/2', value: 2},
    {op: 'add', path: '/a/01', value: 2},
    {op: 'remove', path: '/a/-'},
    {op: 'replace', path: '/a/0'},
    {op: 'test', path: '/nested/x', value: 2},
    {op: 'move', from: '/nested', path: '/nested/inside'},
    {op: 'copy', from: '/absent', path: '/copied'},
    {op: 'something', path: '/nested'},
    {op: 'add', path: 'missing-slash', value: 1},
    {op: 'add', path: '/invalid~2escape', value: 1},
  ]) assert.throws(() => applyPatch(before, [{op: 'replace', path: '/nested/x', value: 8}, operation]));
  assert.equal(before.nested.x, 1);
});

test('目标路径及 from 路径禁止 prototype 污染和继承属性访问', () => {
  for (const path of ['/__proto__/polluted', '/constructor/prototype/polluted', '/nested/prototype', '/toString/x']) {
    assert.throws(() => applyPatch({nested: {}}, [{op: 'add', path, value: true}]));
  }
  assert.throws(() => applyPatch({}, [{op: 'copy', from: '/__proto__', path: '/safe'}]));
  assert.equal({}.polluted, undefined);
});

test('缺少同步或 patch 基线时要求 sync，水位不推进', () => {
  const state = new StreamState('thread');
  assert.equal(state.consume(update(1, 'first', 'running', 'add')).kind, 'resync');
  assert.equal(state.heads.size, 0);
  assert.equal(state.consume(sync()).kind, 'sync');
  assert.equal(state.consume(update(1, 'first')).kind, 'resync');
  assert.equal(state.heads.get('task'), 0);
});

test('同步后连续应用，旧帧和重复帧忽略，缺帧不污染消息副本', () => {
  const state = new StreamState('thread');
  state.consume(sync());
  assert.equal(state.consume(update(1, 'first', 'running', 'add')).kind, 'update');
  assert.equal(state.consume(update(2, 'second')).kind, 'update');
  assert.equal(state.consume(update(2, 'duplicate')).kind, 'ignore');
  assert.equal(state.consume(update(1, 'old')).kind, 'ignore');
  assert.equal(state.consume(update(4, 'gap')).kind, 'resync');
  assert.equal(state.consume(update(3, 'duplicate add', 'running', 'add')).kind, 'resync');
  assert.equal(first(state).meta_data.content, 'second');
  assert.equal(state.heads.get('task'), 2);
});

test('同步原子替换全部页面消息；拒绝旧快照和无效快照', () => {
  const state = new StreamState('thread');
  state.consume(sync(2, 'latest'));
  assert.equal(state.consume(sync(1, 'old')).kind, 'resync');
  assert.equal(first(state).meta_data.content, 'latest');
  const broken = sync(3, 'broken'); broken.messages[0].value.status = 'invalid';
  assert.equal(state.consume(broken).kind, 'resync');
  assert.equal(state.heads.get('task'), 2);
  state.consume(sync(3));
  assert.equal(state.items.size, 0);
  assert.equal(state.heads.get('task'), 3);
});

test('同步的消息字典顺序不影响显示，按任务及消息 index 排列', () => {
  const state = new StreamState('thread');
  const frame = sync(2, 'answer');
  frame.messages.push({task_id: 'task', msg_id: 'user', value: {...item('question'), sender: 'user', index: 0}});
  state.consume(frame);
  assert.deepEqual(state.orderedItems.map(([, value]) => value.meta_data.content), ['question', 'answer']);
});

test('错误 thread、错配水位不应用；新任务 seq1 可以建立新消息基线', () => {
  const state = new StreamState('thread');
  state.consume(sync());
  const wrong = update(1, 'wrong', 'running', 'add'); wrong.event_v2.thread_id = 'old-thread';
  assert.equal(state.consume(wrong).kind, 'ignore');
  assert.equal(state.consume({...sync(), thread_id: 'other'}).kind, 'ignore');
  const mismatch = update(1, 'wrong', 'running', 'add'); mismatch.stream.task_id = 'other';
  assert.equal(state.consume(mismatch).kind, 'resync');
  const next = update(1, 'next', 'running', 'add');
  next.event_v2.task_id = next.stream.task_id = 'next-task';
  assert.equal(state.consume(next).kind, 'update');
  assert.equal(state.activeTask.task_id, 'next-task');
});

test('失败 patch 不推进水位；终态不被迟到接纳回复或旧同步覆盖', () => {
  const state = new StreamState('thread');
  state.consume(sync(1, 'first'));
  const broken = update(2, 'second'); broken.event_v2.patches.push({op: 'test', path: '/index', value: 9});
  assert.equal(state.consume(broken).kind, 'resync');
  assert.equal(first(state).meta_data.content, 'first');
  assert.equal(state.heads.get('task'), 1);
  state.consume(update(2, 'done', 'complete'));
  state.acceptTask({thread_id: 'thread', task_id: 'task', status: 'running'});
  assert.equal(state.activeTask, undefined);
  assert.equal(state.latestTask.status, 'complete');
  assert.equal(state.consume(sync(2, 'first', 'running')).kind, 'resync');
  assert.equal(state.latestTask.status, 'complete');
});

test('heads只比对，发现缺失任务、状态或序号后请求同步', () => {
  const state = new StreamState('thread');
  state.consume(sync(1, 'first'));
  assert.equal(state.consume({...sync(1), type: 'heads'}).kind, 'ignore');
  assert.equal(state.consume({...sync(2, 'latest', 'complete'), type: 'heads'}).kind, 'resync');
  assert.equal(state.heads.get('task'), 1);
  assert.equal(first(state).meta_data.content, 'first');
  assert.equal(state.consume({...sync(1, 'latest', 'failed'), type: 'heads'}).kind, 'resync');
  assert.equal(state.consume({type: 'heads', thread_id: 'thread', heads: {other: 0}, tasks: []}).kind, 'resync');
  const saving = {...sync(1), type: 'heads'}; saving.tasks[0].save_pending = true;
  assert.equal(state.consume(saving).kind, 'resync');
  assert.equal(state.heads.get('task'), 1);
  state.consume({...saving, type: 'sync'});
  assert.equal(state.latestTask.save_pending, true);
  assert.equal(state.activeTask.task_id, 'task');
});

test('新连接接受新server_epoch的中断恢复检查点，允许seq10回退到failed6', () => {
  const state = new StreamState('thread');
  state.consume(sync(10, '崩溃前浏览器已收到的文本'));
  state.beginConnection();
  const recovered = {...sync(6, '恢复到Redis检查点', 'failed'), server_epoch: 'epoch-b'};
  recovered.tasks[0].error = 'INTERRUPTED：服务重启，执行已中断；请重新提交';
  assert.equal(state.consume(recovered).kind, 'sync');
  assert.equal(state.heads.get('task'), 6);
  assert.equal(first(state).meta_data.content, '恢复到Redis检查点');
  assert.equal(state.latestTask.status, 'failed');
  assert.equal(state.activeTask, undefined);
  assert.equal(state.serverEpoch, 'epoch-b');
  assert.equal(state.connectionEpoch, 'epoch-b');
});

test('同epoch的新连接仍拒绝水位回退，未确认中断的新epoch也不能回退', () => {
  const state = new StreamState('thread');
  state.consume(sync(10, 'latest'));
  state.beginConnection();
  const old = sync(6, 'old', 'failed'); old.tasks[0].error = 'INTERRUPTED：旧检查点';
  assert.equal(state.consume(old).kind, 'resync');
  const running = {...sync(6, 'old'), server_epoch: 'epoch-b'};
  assert.equal(state.consume(running).kind, 'resync');
  const failed = {...sync(6, 'old', 'failed'), server_epoch: 'epoch-b'};
  failed.tasks[0].error = '其他错误';
  assert.equal(state.consume(failed).kind, 'resync');
  assert.equal(state.heads.get('task'), 10);
  assert.equal(state.serverEpoch, 'epoch-a');
});

test('同连接首个有效sync锁定epoch，迟到旧epoch或heads不能获得切换许可', () => {
  const state = new StreamState('thread');
  state.consume(sync(10, 'latest'));
  const recovered = {...sync(6, 'checkpoint', 'failed'), server_epoch: 'epoch-b'};
  recovered.tasks[0].error = 'INTERRUPTED：服务已重启';
  assert.equal(state.consume({...recovered, type: 'heads'}).kind, 'resync');
  assert.equal(state.serverEpoch, 'epoch-a');
  assert.equal(state.heads.get('task'), 10);
  assert.equal(state.consume(recovered).kind, 'resync');
  state.beginConnection();
  assert.equal(state.consume(recovered).kind, 'sync');
  assert.equal(state.consume(sync(11, '迟到旧进程')).kind, 'resync');
  assert.equal(state.serverEpoch, 'epoch-b');
  assert.equal(state.heads.get('task'), 6);
  assert.equal(first(state).meta_data.content, 'checkpoint');
});
