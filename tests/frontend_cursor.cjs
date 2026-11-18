// Exercise the actual dashboard functions with reordered REST/SSE delivery.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../static/index.html'), 'utf8');
function extract(name) {
  const asyncIdx = html.indexOf(`    async function ${name}(`);
  const syncIdx = html.indexOf(`    function ${name}(`);
  const start = asyncIdx >= 0 ? asyncIdx : syncIdx;
  assert.ok(start >= 0, `Could not find function ${name}`);
  const remainder = html.slice(start);
  const end = remainder.slice(1).search(/\n    (?:async )?function /);
  assert.ok(end >= 0, `Could not find end of function ${name}`);
  return remainder.slice(0, end + 1);
}

function makeFakeElement(tag) {
  const listeners = new Map();
  const classes = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    className: '',
    textContent: '',
    innerHTML: '',
    title: '',
    isConnected: false,
    options: [],
    classList: {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (n, f) => (f === undefined ? !classes.has(n) : f) ? classes.add(n) : classes.delete(n),
      contains: (n) => classes.has(n),
    },
    appendChild(child) {
      el.removeChild(child);
      child.parentNode = el;
      child.isConnected = el.isConnected;
      el.children.push(child);
      if (child.tagName === 'OPTION') el.options.push(child);
      return child;
    },
    insertBefore(child, ref) {
      el.removeChild(child);
      child.parentNode = el;
      child.isConnected = el.isConnected;
      if (!ref) {
        el.children.push(child);
        return child;
      }
      const idx = el.children.indexOf(ref);
      if (idx === -1) el.children.push(child);
      else el.children.splice(idx, 0, child);
      return child;
    },
    removeChild(child) {
      const idx = el.children.indexOf(child);
      if (idx !== -1) el.children.splice(idx, 1);
      if (child.parentNode === el) child.parentNode = null;
    },
    remove() {
      if (el.parentNode) el.parentNode.removeChild(el);
      el.isConnected = false;
    },
    get nextSibling() {
      if (!el.parentNode) return null;
      const idx = el.parentNode.children.indexOf(el);
      return idx >= 0 && idx + 1 < el.parentNode.children.length ? el.parentNode.children[idx + 1] : null;
    },
    addEventListener(event, fn) {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event).push(fn);
    },
    click() {
      for (const fn of listeners.get('click') || []) fn();
    },
    querySelectorAll(selector) {
      const cls = selector.startsWith('.') ? selector.slice(1) : null;
      const out = [];
      const walk = (node) => {
        for (const c of node.children) {
          if (cls && (c.className.split(/\s+/).includes(cls) || c.classList.contains(cls))) {
            out.push(c);
          }
          walk(c);
        }
      };
      walk(el);
      return out;
    },
    querySelector(selector) {
      return el.querySelectorAll(selector)[0] || null;
    },
  };
  return el;
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

const domElements = new Map();
const boardEl = makeFakeElement('main');
boardEl.isConnected = true;
const emptyStateEl = makeFakeElement('div');
const taskFilterEl = makeFakeElement('select');
domElements.set('board', boardEl);
domElements.set('empty-state', emptyStateEl);
domElements.set('task-filter', taskFilterEl);

const deletedIds = [];
const copiedTexts = [];
const domContext = vm.createContext({
  console,
  setTimeout,
  clearTimeout,
  document: {
    createElement: (tag) => makeFakeElement(tag),
    getElementById: (id) => {
      if (!domElements.has(id)) domElements.set(id, makeFakeElement('div'));
      return domElements.get(id);
    },
  },
  window: { innerHeight: 800, scrollY: 0, scrollTo: () => {} },
  board: boardEl,
  emptyState: emptyStateEl,
  nearBottom: () => false,
  formatTime: () => '12:00:00',
  renderMarkdown: (c) => `<p>${c}</p>`,
  copyToClipboard: async (text) => { copiedTexts.push(text); return true; },
  deleteMessage: (id) => { deletedIds.push(id); },
});
vm.runInContext(`
  let myRole = 'master', oldestId = 0, knownIds = new Set();
  let currentTaskFilter = '', currentMessageTypeFilter = '', currentStatusFilter = '';
  let currentActionFilter = '', currentApprovalFilter = '', currentTypedKindFilter = '', currentCorrelationFilter = '';
  ${extract('normalizeRole')}
  ${extract('flashCopyButton')}
  ${extract('registerTaskId')}
  ${extract('createBadge')}
  ${extract('applyFiltersToElement')}
  ${extract('buildTypedDetails')}
  ${extract('buildTypedIntegrityDetails')}
  ${extract('buildMessageElement')}
  ${extract('appendMessage')}
`, domContext);

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

  // Verify buildMessageElement button wiring, role alias normalization, and sorted appendMessage insertion
  const built = vm.runInContext(
    "buildMessageElement({ id: 10, role: 'engineer', content: 'Order text', created_at: 1, order_id: 'ORD-1' })",
    domContext
  );
  const copyBtn = built.querySelector('.action-btn');
  const deleteBtn = built.querySelector('.delete-btn');
  assert.ok(copyBtn, 'Copy button must have .action-btn class');
  assert.ok(deleteBtn, 'Master user must see delete button on legacy engineer message');
  copyBtn.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(copiedTexts.length, 1, 'Copy button must copy text');
  assert.deepEqual(deletedIds, [], 'Clicking copy button must never trigger deleteMessage');
  deleteBtn.click();
  assert.deepEqual(deletedIds, [10], 'Clicking delete button must trigger deleteMessage');

  vm.runInContext("appendMessage({ id: 1, role: 'master', content: 'm1', created_at: 1 })", domContext);
  vm.runInContext("appendMessage({ id: 5, role: 'master', content: 'm5', created_at: 5 })", domContext);
  vm.runInContext("appendMessage({ id: 4, role: 'agent', content: 'm4', created_at: 4 })", domContext);
  const renderedIds = boardEl.querySelectorAll('.message').map(el => Number(el.dataset.id));
  assert.deepEqual(renderedIds, [1, 4, 5], 'appendMessage must insert out-of-order messages sorted by id');

  console.log('Dashboard cursor recovery verified for reordered POST/SSE delivery.');
})().catch(error => { console.error(error); process.exitCode = 1; });
