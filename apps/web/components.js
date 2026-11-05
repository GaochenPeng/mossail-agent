// 接收协议数据，在组件边界显式映射字段；新增展示类型在此注册。
const renderers = new Map();

export function registerMessage(type, renderer) {
  if (renderers.has(type)) throw new Error('消息组件重复注册：' + type);
  renderers.set(type, renderer);
}

function markdown(node, {content, source = []}, item, taskStatus) {
  const text = document.createElement('div');
  const ended = ['complete', 'failed', 'abandon'].includes(taskStatus);
  text.textContent = content || (ended ? (taskStatus === 'abandon' ? '已停止，未生成结果' : '未生成结果') : '正在生成…');
  node.replaceChildren(text);
  if (!Array.isArray(source) || !source.length) return;
  const list = document.createElement('ul');
  for (const item of source) {
    let url;
    try { url = new URL(item?.url); } catch { continue; }
    if (!['http:', 'https:'].includes(url.protocol)) continue;
    const li = document.createElement('li'), link = document.createElement('a');
    link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.textContent = '[' + item.id + '] ' + item.title;
    li.append(link); list.append(li);
  }
  node.append(list);
}

registerMessage('markdown', markdown);
registerMessage('tool_call', (node, {name, arguments: args, output}, item, taskStatus) => {
  let state = {loading: '执行中', success: '已完成', error: '失败'}[item.status] || item.status;
  if (item.status === 'loading') state = {complete: '未返回结果', failed: '已中止', abandon: '已停止'}[taskStatus] || state;
  const show = value => typeof value === 'string' ? value : JSON.stringify(value);
  const details = document.createElement('details'), summary = document.createElement('summary'), body = document.createElement('pre');
  summary.textContent = '工具：' + name + ' · ' + state;
  body.textContent = '参数：' + (show(args) || '')
    + (output !== undefined ? '\n结果：' + show(output) : '');
  details.append(summary, body);
  node.append(details);
});

export function renderMessage(node, item, taskStatus) {
  node.dataset.sender = item.sender;
  node.dataset.status = item.status;
  node.className = item.type === 'tool_call' ? 'tool' : '';
  node.replaceChildren();
  const renderer = renderers.get(item.type);
  if (renderer) renderer(node, item.meta_data || {}, item, taskStatus);
  else node.textContent = '未注册消息类型：' + item.type;
}
