'use strict';
const test = require('node:test');
const assert = require('node:assert');
const nock = require('nock');
const zapier = require('zapier-platform-core');
const App = require('../index');
const nodes = require('./fixtures/nodes.json');

const { createHash } = require('node:crypto');
const appTester = zapier.createAppTester(App);
// the pod-name code for a rental: key, GPU model, GPU count, SSH key, template
const code = (key, gpu = 'H100', count = 1, ssh = 'ssh-ed25519 AAAA test', template = null) =>
  createHash('sha256').update(JSON.stringify([key, gpu, count, ssh, template])).digest('hex').slice(0, 12);
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
  nock('https://lium.io').get('/api/pods').reply(200, [{ id: 'old', pod_name: 'zap-other' }]);
  nock('https://lium.io', { reqheaders: { 'Idempotency-Key': 'evt-1' } })
    .post('/api/executors/rent-by-spec', (b) => ((sent = b), true))
    .reply(200, { pod_id: 'p9', price_per_hour: 2.5, template_id: 't1', selected_executor: { id: 'n1', machine_name: 'm1', gpu_count: 8, price_per_gpu: 2.0, available_gpu_count: 3 } });
  const out = await appTester(App.creates.rent_gpu_pod.operation.perform, {
    authData,
    inputData: { gpu_type: 'H100', ssh_public_key: ' ssh-ed25519 AAAA me ', max_price_per_gpu_hour: '3', idempotency_key: 'evt-1' },
  });
  assert.deepStrictEqual(sent, {
    gpu_type: 'H100', gpu_count: 1, pod_name: `zap-${code('evt-1', 'H100', 1, 'ssh-ed25519 AAAA me')}`, user_public_key: ['ssh-ed25519 AAAA me'], max_price_per_gpu_hour: 3,
  });
  assert.strictEqual(out.id, 'p9');
  assert.strictEqual(out.replayed, false);
  // the charged price, not the catalogue's 2.0, as a replay of this rental reports
  assert.deepStrictEqual(out.node, { id: 'n1', machine_name: 'm1', gpu_count: 8, price_per_gpu: 2.5 });
});

const rent = App.creates.rent_gpu_pod.operation.perform;
// A fake z: GET /pods lists `pods`, every POST goes to `post`.
const fakeZ = (post, pods = []) => ({ request: async (req) => (req.method === 'POST' ? post(req) : { data: pods }) });
const rentInput = { gpu_type: 'H100', ssh_public_key: 'ssh-ed25519 AAAA test', idempotency_key: 'upstream-event-42' };

test('rent: a replay after a lost response returns the first pod, not a second', async () => {
  let created = 0;
  const saved = new Map();
  const bodies = [];
  const z = fakeZ(async ({ headers = {}, body }) => {
    bodies.push(JSON.stringify(body));
    const key = headers['Idempotency-Key'];
    if (key && saved.has(key)) return { data: saved.get(key) };
    const data = { pod_id: `p${++created}`, price_per_hour: 2 };
    if (key) saved.set(key, data);
    if (created === 1) throw Error('response lost after commit');
    return { data };
  });
  const bundle = { inputData: rentInput };
  await rent(z, bundle).catch(() => {});
  const replay = await rent(z, bundle);
  assert.strictEqual(created, 1);
  assert.strictEqual(replay.pod_id, 'p1');
  assert.strictEqual(bodies[0], bodies[1]); // identical body, or the backend refuses the key
});

test('rent: a key outside the backend charset is hashed the same way every time', async () => {
  const keys = [];
  const z = fakeZ(async ({ headers }) => (keys.push(headers['Idempotency-Key']), { data: { pod_id: 'p1' } }));
  for (let i = 0; i < 2; i++) await rent(z, { inputData: { ...rentInput, idempotency_key: 'zap run/42 #1' } });
  assert.match(keys[0], /^[0-9a-f]{64}$/);
  assert.strictEqual(keys[0], keys[1]);
});

test('rent: price cap 0 or invalid is refused before the POST; missing is omitted; positive is sent', async () => {
  for (const [cap, expected] of [[0, 'refused'], ['0', 'refused'], [-1, 'refused'], ['abc', 'refused'], [Infinity, 'refused'],
    [undefined, 'omitted'], [null, 'omitted'], ['', 'omitted'], [2.5, 2.5], ['3', 3]]) {
    let sent;
    const z = fakeZ(async ({ body }) => ((sent = body), { data: { pod_id: 'p1' } }));
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
  const z = { request: async () => ((posted = true), { data: [] }) };
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

test('rent: a replay after the 24 h key window returns the pod already named for this key, without renting', async () => {
  let posts = 0;
  const pods = [{ id: 'p1', pod_name: `zap-${code(rentInput.idempotency_key)}`, status: 'RUNNING', price: 2.5,
    template: { id: 't1' }, executor: { id: 'n1', machine_name: 'm1', gpu_count: 8, price_per_gpu: null } }];
  const out = await rent(fakeZ(async () => (posts++, { data: { pod_id: 'p2' } }), pods), { inputData: rentInput });
  assert.strictEqual(posts, 0);
  assert.strictEqual(out.pod_id, 'p1');
  assert.strictEqual(out.replayed, true);
  // the same output fields as a fresh rental
  // /pods has no per-GPU price: it comes from the pod's total over the rented GPU count
  assert.deepStrictEqual([out.price_per_hour, out.template_id, out.node],
    [2.5, 't1', { id: 'n1', machine_name: 'm1', gpu_count: 8, price_per_gpu: 2.5 }]);
  // a custom prefix is kept in front of the key code
  const named = await rent(fakeZ(async () => (posts++, { data: { pod_id: 'p3' } }), [{ id: 'p4', pod_name: `train-${code('k')}` }]),
    { inputData: { ...rentInput, idempotency_key: 'k', pod_name: 'train' } });
  assert.strictEqual(named.pod_id, 'p4');
  assert.strictEqual(posts, 0);
  // the same key on another rental (other GPU, other SSH key) is not handed the first pod
  const other = await rent(fakeZ(async () => (posts++, { data: { pod_id: 'p5' } }), pods),
    { inputData: { ...rentInput, gpu_type: 'RTX4090', ssh_public_key: 'ssh-ed25519 BBBB other' } });
  assert.strictEqual(other.pod_id, 'p5');
  assert.strictEqual(posts, 1);
});

test('rent: a 409 for an in-flight or unrecorded earlier call is a clear error telling the user to check their pods', async () => {
  for (const code of ['idempotency_in_progress', 'idempotency_outcome_unknown']) {
    let threw = false;
    const res = { status: 409, data: { error: { code } }, throwForStatus: () => { threw = true; } };
    await assert.rejects(rent(fakeZ(async () => res), { inputData: rentInput }), (e) => {
      assert.ok(e instanceof zapier.errors.Error, code);
      assert.match(e.message, /check your pods/);
      return true;
    });
    assert.strictEqual(threw, false);
  }
  // a 409 that is not an idempotency refusal (no node matches) keeps the platform's normal error
  const noMatch = { status: 409, data: { error: { code: 'no_executor_matches_spec' } }, throwForStatus: () => { throw Error('ResponseError 409'); } };
  await assert.rejects(rent(fakeZ(async () => noMatch), { inputData: rentInput }), /ResponseError 409/);
});
