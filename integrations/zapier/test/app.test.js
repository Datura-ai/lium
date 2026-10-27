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

test('rent is hidden and refuses without calling Lium', async () => {
  const rent = App.creates.rent_gpu_pod;
  assert.strictEqual(rent.display.hidden, true);
  // a migrated step still shows its fields: none may promise a rental
  for (const f of rent.operation.inputFields) assert.match(f.helpText, /^Unused: renting is not available/);
  let called = false;
  await assert.rejects(rent.operation.perform({ request: async () => { called = true; } }, { inputData: {} }), /not available from Zapier/);
  assert.strictEqual(called, false);
});
