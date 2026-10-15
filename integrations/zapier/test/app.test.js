'use strict';
const test = require('node:test');
const assert = require('node:assert');
const nock = require('nock');
const zapier = require('zapier-platform-core');
const App = require('../index');
const nodes = require('./fixtures/nodes.json');

const appTester = zapier.createAppTester(App);
const authData = { api_key: 'test-key' };

test.afterEach(() => nock.cleanAll());

test('find offers filters by model and max price, cheapest first', async () => {
  nock('https://lium.io').get('/api/public/v1/nodes').reply(200, nodes);
  const out = await appTester(App.searches.find_gpu_offers.operation.perform, {
    authData,
    inputData: { gpu_model: 'b', max_price: '7' },
  });
  assert.ok(out.length > 0);
  for (const n of out) {
    assert.match(n.gpu_model.toLowerCase(), /b/);
    assert.ok(n.price_per_gpu_hour <= 7);
  }
  const prices = out.map((n) => n.price_per_gpu_hour);
  assert.deepStrictEqual(prices, [...prices].sort((a, b) => a - b));
});

test('pod trigger sends the API key and returns the pod', async () => {
  nock('https://lium.io', { reqheaders: { 'X-API-KEY': 'test-key' } })
    .get('/api/pods')
    .reply(200, [{ id: 'p1', pod_name: 'run', status: 'RUNNING' }]);
  const out = await appTester(App.triggers.pod_status_changed.operation.perform, { authData });
  assert.deepStrictEqual(out[0], { id: 'p1', pod_id: 'p1', pod_name: 'run', status: 'RUNNING' });
});

test('New Pod keys on the pod id whatever its status', async () => {
  for (const status of ['PENDING', 'RUNNING', 'REBOOT_PENDING', 'RUNNING']) {
    const [item] = await App.triggers.pod_status_changed.operation.perform({
      request: async () => ({ data: [{ id: 'p1', status }] }),
    });
    assert.strictEqual(item.id, 'p1');
    assert.strictEqual(item.pod_id, 'p1');
  }
});

test('new offer: servers seen at activation or before do not trigger again', async () => {
  // Zapier dedupe: the first poll seeds the seen set, later polls emit only unseen ids.
  const polls = [[{ id: 'n1' }], [], [{ id: 'n1' }], [{ id: 'n1' }, { id: 'n2' }]];
  const seen = new Set();
  const emittedCounts = [];
  for (const [i, nodes] of polls.entries()) {
    const out = await App.triggers.new_gpu_offer.operation.perform(
      { request: async () => ({ data: { nodes: nodes.map((n) => ({ ...n, gpu_model: 'H100', price_per_gpu_hour: 2, gpu_count: 1 })) } }) },
      { inputData: {} },
    );
    emittedCounts.push(i === 0 ? 0 : out.filter((n) => !seen.has(n.id)).length);
    out.forEach((n) => seen.add(n.id));
  }
  assert.deepStrictEqual(emittedCounts, [0, 0, 0, 1]);
});

test('offer filter keeps a zero max price (matches nothing priced above 0)', async () => {
  const z = { request: async () => ({ data: { nodes: [{ id: 'n1', gpu_model: 'H100', price_per_gpu_hour: 2, gpu_count: 1 }] } }) };
  for (const [max_price, n] of [[0, 0], ['0', 0], ['', 1], [undefined, 1], [null, 1], ['3', 1]]) {
    const out = await App.searches.find_gpu_offers.operation.perform(z, { inputData: { max_price } });
    assert.strictEqual(out.length, n, `max_price=${max_price}`);
  }
});

test('rent posts a rent-by-spec request and returns the pod id', async () => {
  let sent;
  nock('https://lium.io', { reqheaders: { 'Idempotency-Key': 'evt-1' } })
    .post('/api/executors/rent-by-spec', (b) => ((sent = b), true))
    .reply(200, { pod_id: 'p9', price_per_hour: 2.5, template_id: 't1', selected_executor: { id: 'n1' } });
  const out = await appTester(App.creates.rent_gpu_pod.operation.perform, {
    authData,
    inputData: { gpu_type: 'H100', ssh_public_key: ' ssh-ed25519 AAAA me ', max_price_per_gpu_hour: '3', idempotency_key: 'evt-1' },
  });
  assert.deepStrictEqual(sent, {
    gpu_type: 'H100', gpu_count: 1, pod_name: 'zapier-pod', user_public_key: ['ssh-ed25519 AAAA me'], max_price_per_gpu_hour: 3,
  });
  assert.strictEqual(out.id, 'p9');
});

const rent = App.creates.rent_gpu_pod.operation.perform;
const rentInput = { gpu_type: 'H100', ssh_public_key: 'ssh-ed25519 AAAA test', idempotency_key: 'upstream-event-42' };

test('rent: a replay after a lost response returns the first pod, not a second', async () => {
  let created = 0;
  const saved = new Map();
  const bodies = [];
  const z = { request: async ({ headers = {}, body }) => {
    bodies.push(JSON.stringify(body));
    const key = headers['Idempotency-Key'];
    if (key && saved.has(key)) return { data: saved.get(key) };
    const data = { pod_id: `p${++created}`, price_per_hour: 2 };
    if (key) saved.set(key, data);
    if (created === 1) throw Error('response lost after commit');
    return { data };
  } };
  const bundle = { inputData: rentInput };
  await rent(z, bundle).catch(() => {});
  const replay = await rent(z, bundle);
  assert.strictEqual(created, 1);
  assert.strictEqual(replay.pod_id, 'p1');
  assert.strictEqual(bodies[0], bodies[1]); // identical body, or the backend refuses the key
});

test('rent: a key outside the backend charset is hashed the same way every time', async () => {
  const keys = [];
  const z = { request: async ({ headers }) => (keys.push(headers['Idempotency-Key']), { data: { pod_id: 'p1' } }) };
  for (let i = 0; i < 2; i++) await rent(z, { inputData: { ...rentInput, idempotency_key: 'zap run/42 #1' } });
  assert.match(keys[0], /^[0-9a-f]{64}$/);
  assert.strictEqual(keys[0], keys[1]);
});

test('rent: price cap 0 or invalid is refused before the POST; missing is omitted; positive is sent', async () => {
  for (const [cap, expected] of [[0, 'refused'], ['0', 'refused'], [-1, 'refused'], ['abc', 'refused'], [Infinity, 'refused'],
    [undefined, 'omitted'], [null, 'omitted'], ['', 'omitted'], [2.5, 2.5], ['3', 3]]) {
    let sent;
    const z = { request: async ({ body }) => ((sent = body), { data: { pod_id: 'p1' } }) };
    const run = rent(z, { inputData: { ...rentInput, max_price_per_gpu_hour: cap } });
    if (expected === 'refused') {
      await assert.rejects(run, /above 0/, `cap=${cap}`);
      assert.strictEqual(sent, undefined, `cap=${cap} must not POST`);
    } else {
      await run;
      assert.strictEqual(sent.max_price_per_gpu_hour, expected === 'omitted' ? undefined : expected, `cap=${cap}`);
    }
  }
});

test('rent: a missing idempotency key is refused before the POST', async () => {
  let posted = false;
  const z = { request: async () => ((posted = true), { data: {} }) };
  await assert.rejects(rent(z, { inputData: { ...rentInput, idempotency_key: '' } }), /Idempotency Key/);
  assert.strictEqual(posted, false);
});

test('terminate: accepted, pending and repeated deletion report deletion_requested, never terminated', async () => {
  for (const message of ['Pod deletion started', 'Pod deletion is in progress']) {
    const out = await App.creates.terminate_pod.operation.perform(
      { request: async () => ({ data: { success: true, message } }) },
      { inputData: { pod_id: 'p1' } },
    );
    assert.strictEqual(out.deletion_requested, true);
    assert.notStrictEqual(out.terminated, true);
  }
});
