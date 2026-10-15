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

test('pod trigger sends the API key and keys items on pod id + status', async () => {
  nock('https://lium.io', { reqheaders: { 'X-API-KEY': 'test-key' } })
    .get('/api/pods')
    .reply(200, [{ id: 'p1', pod_name: 'run', status: 'RUNNING' }]);
  const out = await appTester(App.triggers.pod_status_changed.operation.perform, { authData });
  assert.deepStrictEqual(out[0], { id: 'p1:RUNNING', pod_id: 'p1', pod_name: 'run', status: 'RUNNING' });
});

test('rent posts a rent-by-spec request and returns the pod id', async () => {
  let sent;
  nock('https://lium.io')
    .post('/api/executors/rent-by-spec', (b) => ((sent = b), true))
    .reply(200, { pod_id: 'p9', price_per_hour: 2.5, template_id: 't1', selected_executor: { id: 'n1' } });
  const out = await appTester(App.creates.rent_gpu_pod.operation.perform, {
    authData,
    inputData: { gpu_type: 'H100', ssh_public_key: ' ssh-ed25519 AAAA me ', max_price_per_gpu_hour: '3' },
  });
  assert.deepStrictEqual(sent, {
    gpu_type: 'H100', gpu_count: 1, pod_name: 'zapier-pod', user_public_key: ['ssh-ed25519 AAAA me'], max_price_per_gpu_hour: 3,
  });
  assert.strictEqual(out.id, 'p9');
});
