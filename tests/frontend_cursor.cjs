// Exercise the actual dashboard functions with reordered REST/SSE delivery.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../static/index.html'), 'utf8');
function extract(name) {
  const start = html.indexOf(`    async function ${name}(`);
  assert.ok(start >= 0);
  const remainder = html.slice(start);
  const end = remainder.slice(1).search(/\n    (?:async )?function /);
  assert.ok(end >= 0);
  return remainder.slice(0, end + 1);
}
const elements = new Map();
const seen = new Set();
const urls = [];
let history = [{ id: 2 }, { id: 3 }];
const context = vm.createContext({
  URLSearchParams,
  console,
  document: { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { value: id === 'content' ? 'New order' : '', checked: false });
    return elements.get(id);
  } },
  window: {},
  alert: (message) => assert.fail(message),
  appendMessage: message => seen.add(message.id),
  authHeaders: () => ({ Authorization: 'Bearer test' }),
  refreshStatus: async () => {},
  setConnStatus: () => {},
  lock: () => assert.fail('Unexpected lock'),
  EventSource: class {
    constructor() { this.readyState = 1; }
    close() {}
  },
  fetch: async (url, options = {}) => {
    urls.push(url);
    if (url === '/api/stream-ticket') return { ok: true, json: async () => ({ ticket: 'ticket' }) };
    if (options.method === 'POST') return { ok: true, json: async () => ({ id: 3 }) };
    const cursor = Number(new URLSearchParams(url.split('?')[1]).get('after_id'));
    return { ok: true, status: 200, json: async () => history.filter(message => message.id > cursor) };
  },
});
vm.runInContext(`
  let token = 'test', myRole = 'master', lastId = 1, sessionVersion = 0;
  let eventSource = null, streamStarting = false;
  ${extract('postMessage')}
  ${extract('fetchMessages')}
  ${extract('startStream')}
`, context);
(async () => {
  await vm.runInContext('postMessage()', context);
  assert.equal(vm.runInContext('lastId', context), 1, 'POST must not skip unloaded messages');
  await vm.runInContext('fetchMessages(false)', context);
  assert.deepEqual([...seen].sort(), [2, 3]);
  assert.equal(vm.runInContext('lastId', context), 3);
  await vm.runInContext('startStream()', context);
  vm.runInContext('eventSource.onmessage({data: JSON.stringify({id: 5})})', context);
  assert.equal(vm.runInContext('lastId', context), 3, 'SSE must not skip REST history');
  history = [{ id: 4 }, { id: 5 }];
  await vm.runInContext('fetchMessages(false)', context);
  assert.ok(seen.has(4), 'Polling must recover the message preceding the SSE event');
  assert.equal(vm.runInContext('lastId', context), 5);
  assert.ok(urls.some(url => url.includes('after_id=1')));
  assert.ok(urls.some(url => url.includes('after_id=3')));
  let resolveHistory;
  context.fetch = async () => ({
    ok: true, status: 200,
    json: () => new Promise(resolve => { resolveHistory = resolve; }),
  });
  const pending = vm.runInContext('fetchMessages(false)', context);
  await new Promise(resolve => setImmediate(resolve));
  vm.runInContext("sessionVersion++; token = ''; myRole = '';", context);
  resolveHistory([{ id: 7 }]);
  await pending;
  assert.ok(!seen.has(7), 'A response from a locked session must not repopulate the board');
  vm.runInContext('eventSource.onmessage({data: JSON.stringify({id: 8})})', context);
  assert.ok(!seen.has(8), 'Events from a locked session must be ignored');
  console.log('Dashboard cursor recovery verified for reordered POST/SSE delivery.');
})().catch(error => { console.error(error); process.exitCode = 1; });
