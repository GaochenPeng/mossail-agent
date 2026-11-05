import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMessage} from '../../apps/web/components.js';

const node = () => ({textContent: '', className: '', dataset: {}, children: [],
  append(...values) { this.children.push(...values); },
  replaceChildren(...values) { this.children = values; },
});
globalThis.document = {createElement: tagName => ({...node(), tagName})};

test('单条无效引用不会破坏正文及其他引用，禁止可执行 URL', () => {
  const target = node();
  renderMessage(target, {sender: 'assistant', type: 'markdown', status: 'success', meta_data: {
    content: '正文保留', source: [
      {id: 1, title: '坏链接', url: 'not-a-url'},
      {id: 2, title: '可用链接', url: 'https://example.com/page'},
      {id: 3, title: '执行链接', url: 'javascript:alert(1)'},
      null,
    ],
  }});
  assert.equal(target.children[0].textContent, '正文保留');
  const references = target.children[1].children;
  assert.equal(references.length, 1);
  assert.equal(references[0].children[0].href, 'https://example.com/page');
  assert.equal(references[0].children[0].rel, 'noopener noreferrer');
});

test('工具展示运行/成功/失败状态及结构化输出，组件切换清理旧内容', () => {
  const target = node();
  for (const [status, label] of [['loading', '执行中'], ['success', '已完成'], ['error', '失败']]) {
    renderMessage(target, {sender: 'tool', type: 'tool_call', status,
      meta_data: {name: 'search', arguments: {q: '问题'}, output: 0}});
    const details = target.children[0];
    assert.equal(details.tagName, 'details');
    assert.equal(details.open ?? false, false);
    assert.match(details.children[0].textContent, new RegExp(label, 'u'));
    assert.match(details.children[1].textContent, /结果：0/u);
    assert.equal(target.className, 'tool');
  }
  renderMessage(target, {sender: 'assistant', type: 'markdown', status: 'success', meta_data: {content: '已完成'}});
  assert.equal(target.className, '');
  assert.equal(target.children[0].textContent, '已完成');
});

test('任务失败或取消后，空答案和loading工具不再显示正在生成或执行中，原数据不变', () => {
  const target = node();
  const message = {sender: 'assistant', type: 'markdown', status: 'loading', meta_data: {content: ''}};
  const before = structuredClone(message);
  for (const taskStatus of ['failed', 'abandon', 'complete']) {
    renderMessage(target, message, taskStatus);
    assert.doesNotMatch(target.children[0].textContent, /正在生成/u);
    assert.match(target.children[0].textContent, /未生成结果/u);
  }
  renderMessage(target, {...message, meta_data: {content: '保留部分答案'}}, 'failed');
  assert.equal(target.children[0].textContent, '保留部分答案');
  assert.deepEqual(message, before);
  const output = '完整输出'.repeat(3000);
  const tool = {sender: 'tool', type: 'tool_call', status: 'loading', meta_data: {name: 'search', arguments: '{}', output}};
  for (const [taskStatus, label] of [['failed', '已中止'], ['abandon', '已停止'], ['complete', '未返回结果']]) {
    renderMessage(target, tool, taskStatus);
    assert.match(target.children[0].children[0].textContent, new RegExp(label, 'u'));
    assert.doesNotMatch(target.children[0].children[0].textContent, /已完成|执行中/u);
    assert.equal(target.children[0].children[1].textContent, '参数：{}\n结果：' + output);
    assert.equal(target.children[0].open ?? false, false);
  }
  assert.equal(tool.status, 'loading');
});
