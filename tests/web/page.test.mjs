import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {StreamState} from '../../apps/web/stream-state.js';

const html = await readFile(new URL('../../apps/web/index.html', import.meta.url), 'utf8');
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/u)[1].replace(/^import .*;\n/gmu, '');
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const item = content => ({index: 1, sender: 'assistant', type: 'markdown', status: 'loading', meta_data: {content}});
const snapshot = (status, seq = 1, threadId = 'thread', savePending = false) => ({
  type: 'sync', thread_id: threadId, server_epoch: 'epoch-a',
  heads: status ? {task: seq} : {},
  tasks: status ? [{task_id: 'task', status, seq, error: null, save_pending: savePending}] : [],
  messages: status ? [{task_id: 'task', msg_id: 'answer', value: item('answer')}] : [],
});

async function page(initialStatus, savePending = false) {
  const elements = new Map();
  const node = () => ({textContent: '', value: '', disabled: false, dataset: {}, children: [],
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  });
  for (const id of ['submit', 'stop', 'new', 'agent', 'question', 'form', 'messages', 'status', 'notice', 'health', 'retry-save']) elements.set(id, node());
  elements.get('agent').value = 'assistant'; elements.get('question').value = '计算一次';
  const storage = initial => { const values = new Map(Object.entries(initial)); return {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
  }; };
  const localStorage = storage({'open-repo-thread': 'thread'}), sessionStorage = storage({});
  const sockets = [], timers = new Map(), requests = [];
  let timerId = 0, threadCounter = 0;
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(payload) { assert.equal(this.readyState, 1); this.sent.push(JSON.parse(payload)); }
    open() { this.readyState = 1; this.onopen(); }
    emit(frame) { this.onmessage({data: JSON.stringify(frame)}); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  const context = vm.createContext({
    StreamState, WebSocket: Socket, structuredClone, localStorage, sessionStorage,
    crypto: {randomUUID: () => 'request-id'}, location: {protocol: 'http:', host: 'localhost'},
    document: {getElementById: id => elements.get(id), createElement: node, createDocumentFragment: node},
    renderMessage: (target, value) => { target.textContent = value.meta_data.content; },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: async (path, options) => {
      requests.push({path, options});
      const value = path === '/threads' ? {thread_id: 'thread-' + (++threadCounter)}
        : path === '/health' ? {agent: {model_configured: true, agents: ['assistant', 'deep_search']}} : [];
      return {ok: true, status: 200, json: async () => value};
    },
  });
  new vm.Script(script, {filename: 'index.html'}).runInContext(context);
  await flush();
  const current = sockets.at(-1); current.open(); current.emit(snapshot(initialStatus, 1, 'thread', savePending));
  return {elements, sockets, requests, sessionStorage, localStorage,
    reconnect() {
      sockets.at(-1).close();
      const timer = [...timers.values()].find(value => value.delay === 1000);
      assert.ok(timer); timer.callback();
      sockets.at(-1).open();
      return sockets.at(-1);
    },
  };
}

test('完成后重连仍显示已完成，收到 sync 后恢复发送按钮', async () => {
  const app = await page('complete');
  assert.equal(app.elements.get('submit').disabled, false);
  const socket = app.reconnect();
  assert.match(app.elements.get('status').textContent, /已完成/u);
  assert.equal(app.elements.get('submit').disabled, true);
  socket.emit(snapshot('complete'));
  assert.equal(app.elements.get('submit').disabled, false);
  assert.equal(app.elements.get('stop').disabled, true);
});

test('未确认提交在断线重试时沿用同一个 client_request_id', async () => {
  const app = await page();
  app.elements.get('form').onsubmit({preventDefault() {}});
  const first = app.sockets.at(-1).sent.find(frame => frame.type === 'chat');
  assert.equal(first.client_request_id, 'request-id');
  assert.equal(app.elements.get('submit').disabled, true);
  const socket = app.reconnect();
  socket.emit(snapshot());
  assert.deepEqual(socket.sent.find(frame => frame.type === 'chat'), first);
  socket.emit({type: 'task_accepted', thread_id: 'thread', task_id: 'task', status: 'submitted', client_request_id: first.client_request_id});
  assert.equal(app.sessionStorage.getItem('mossail-pending'), null);
  assert.equal(app.elements.get('submit').disabled, true);
});

test('sync 可以确认已接纳提交，不重复发送；迟到接纳不会复活完成任务', async () => {
  const app = await page();
  app.elements.get('form').onsubmit({preventDefault() {}});
  const socket = app.reconnect();
  const frame = snapshot('complete'); frame.tasks[0].client_request_id = 'request-id';
  socket.emit(frame);
  assert.equal(socket.sent.some(value => value.type === 'chat'), false);
  socket.emit({type: 'task_accepted', thread_id: 'thread', task_id: 'task', status: 'running', client_request_id: 'request-id'});
  assert.equal(app.elements.get('stop').disabled, true);
  assert.equal(app.elements.get('submit').disabled, false);
});

test('一般错误和记录错误仅请求同步，不错误解除运行任务', async () => {
  const app = await page('running');
  const socket = app.sockets.at(-1);
  for (const type of ['error', 'recording_error']) {
    socket.emit({type, message: '暂时失败'});
    assert.equal(app.elements.get('submit').disabled, true);
    assert.match(app.elements.get('status').textContent, /执行中/u);
    socket.emit(snapshot('running', 1, 'thread', type === 'recording_error'));
    assert.equal(app.elements.get('submit').disabled, true);
    assert.equal(app.elements.get('stop').disabled, type === 'recording_error');
  }
});

test('运行中新建会话先停止，等终态确认后创建，旧 socket 和旧 thread 帧忽略', async () => {
  const app = await page('running');
  const old = app.sockets.at(-1);
  app.elements.get('new').onclick();
  assert.equal(old.sent.at(-1).type, 'stop');
  assert.equal(app.requests.filter(request => request.path === '/threads').length, 0);
  old.emit({protocol_version: 2, stream: {task_id: 'task', seq: 2}, event_v2: {
    thread_id: 'thread', task_id: 'task', msg_id: 'answer', task_status: 'abandon', op: 'replace', value: item('stopped'),
  }});
  await flush();
  assert.equal(app.requests.filter(request => request.path === '/threads').length, 1);
  assert.equal(app.localStorage.getItem('open-repo-thread'), 'thread-1');
  const current = app.sockets.at(-1); current.open(); current.emit(snapshot(undefined, 0, 'thread-1'));
  old.emit(snapshot('running', 10));
  current.emit(snapshot('running', 10));
  assert.equal(app.elements.get('submit').disabled, false);
  assert.equal(app.elements.get('stop').disabled, true);
  assert.equal(app.elements.get('messages').children[0].children.length, 0);
});

test('序号缺口只请求同步，恢复完整快照后继续接收', async () => {
  const app = await page('running');
  const socket = app.sockets.at(-1);
  const gap = {protocol_version: 2, stream: {task_id: 'task', seq: 3}, event_v2: {
    thread_id: 'thread', task_id: 'task', msg_id: 'answer', op: 'replace', task_status: 'complete', value: item('done'),
  }};
  socket.emit(gap); socket.emit(gap);
  assert.equal(socket.sent.filter(frame => frame.type === 'sync_request').length, 1);
  assert.equal(app.elements.get('submit').disabled, true);
  socket.emit(snapshot('complete', 3));
  assert.equal(app.elements.get('submit').disabled, false);
});

test('心跳发现最后一帧丢失时请求完整同步，不直接提升本地水位', async () => {
  const app = await page('running');
  const socket = app.sockets.at(-1);
  socket.emit({...snapshot('complete', 2), type: 'heads', messages: undefined});
  assert.equal(socket.sent.at(-1).type, 'sync_request');
  assert.match(app.elements.get('status').textContent, /执行中/u);
  assert.equal(app.elements.get('submit').disabled, true);
  socket.emit(snapshot('complete', 2));
  assert.equal(app.elements.get('submit').disabled, false);
});

test('记录失败保留运行锁，重试只调用保存端点，确认终态后解锁', async () => {
  const app = await page('running');
  const socket = app.sockets.at(-1);
  socket.emit({type: 'recording_error', thread_id: 'thread', task_id: 'task', message: '终态保存失败'});
  socket.emit(snapshot('running', 1, 'thread', true));
  assert.equal(app.elements.get('retry-save').hidden, false);
  assert.equal(app.elements.get('submit').disabled, true);
  await app.elements.get('retry-save').onclick();
  assert.equal(app.requests.at(-1).path, '/tasks/task/retry-save');
  assert.equal(app.requests.at(-1).options.method, 'POST');
  assert.equal(app.elements.get('submit').disabled, true);
  socket.emit(snapshot('complete', 2));
  assert.equal(app.elements.get('retry-save').hidden, true);
  assert.equal(app.elements.get('submit').disabled, false);
});

test('刷新错过 recording_error 时，从完整同步恢复保存按钮及任务锁', async () => {
  const app = await page('running', true);
  assert.equal(app.elements.get('retry-save').hidden, false);
  assert.equal(app.elements.get('submit').disabled, true);
  assert.equal(app.elements.get('stop').disabled, true);
  assert.match(app.elements.get('status').textContent, /等待保存/u);
  await app.elements.get('retry-save').onclick();
  app.sockets.at(-1).emit(snapshot('complete', 2));
  assert.equal(app.elements.get('retry-save').hidden, true);
  assert.equal(app.elements.get('notice').textContent, '');
  assert.equal(app.elements.get('submit').disabled, false);
});

test('heads同seq保存标记变化触发同步，停止回复也以完整同步确认状态', async () => {
  const app = await page('running');
  const socket = app.sockets.at(-1);
  app.elements.get('stop').onclick();
  socket.emit({type: 'cancel_result', task_id: 'task', cancelled: false, status: 'running', message: '正在处理记录保存'});
  assert.equal(socket.sent.at(-1).type, 'sync_request');
  assert.equal(app.elements.get('submit').disabled, true);
  socket.emit(snapshot('running'));
  socket.emit({...snapshot('running', 1, 'thread', true), type: 'heads', messages: undefined});
  assert.equal(socket.sent.at(-1).type, 'sync_request');
  socket.emit(snapshot('running', 1, 'thread', true));
  assert.equal(app.elements.get('retry-save').hidden, false);
  assert.equal(app.elements.get('stop').disabled, true);
  assert.equal(app.elements.get('submit').disabled, true);
});

test('进程重启后接受新epoch的较低中断检查点，旧socket迟到帧不能覆盖', async () => {
  const app = await page('running');
  const old = app.sockets.at(-1);
  old.emit(snapshot('running', 10));
  const current = app.reconnect();
  const recovered = {...snapshot('failed', 6), server_epoch: 'epoch-b'};
  recovered.tasks[0].error = 'INTERRUPTED：服务重启，执行已中断';
  recovered.messages[0].value.meta_data.content = '恢复的检查点正文';
  current.emit(recovered);
  assert.equal(app.elements.get('submit').disabled, false);
  assert.equal(app.elements.get('stop').disabled, true);
  assert.match(app.elements.get('status').textContent, /执行失败/u);
  assert.match(app.elements.get('notice').textContent, /INTERRUPTED.*检查点/u);
  old.emit(snapshot('running', 11));
  assert.equal(app.elements.get('submit').disabled, false);
  assert.match(app.elements.get('status').textContent, /执行失败/u);
  assert.equal(app.elements.get('messages').children[0].children[0].textContent, '恢复的检查点正文');
});
