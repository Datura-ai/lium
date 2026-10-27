'use strict';

// Lium (lium.io) is a GPU cloud: rent GPU servers (H100, B200, RTX 4090 ...) by the hour.
// This app wraps Lium's REST API (https://lium.io/api), the same API the `lium` CLI and SDK use.
const { version } = require('./package.json');
const { version: platformVersion, errors } = require('zapier-platform-core');
const { createHash } = require('node:crypto');

const API = 'https://lium.io/api';
const NODES_FEED = `${API}/public/v1/nodes`; // public, no API key needed

// Only undefined, null and '' mean "not set": a numeric 0 is a real (zero) cap.
const present = (v) => v !== undefined && v !== null && v !== '';

const addApiKey = (request, z, bundle) => {
  if (bundle.authData && bundle.authData.api_key && request.url.startsWith(API)) {
    request.headers = { ...request.headers, 'X-API-KEY': bundle.authData.api_key };
  }
  return request;
};

const authentication = {
  type: 'custom',
  fields: [
    {
      key: 'api_key',
      label: 'API Key',
      required: true,
      type: 'password',
      helpText:
        'Your Lium API key. Create one at https://lium.io/api-keys.',
    },
  ],
  test: { url: `${API}/users/me` },
  connectionLabel: '{{email}}',
};

// ---- GPU offers (public feed) ----

const offerSample = {
  id: '80cc7015-a0cf-40b9-8c0a-ba1c1eec30f6',
  gpu_model: 'B200',
  gpu_count: 1,
  available_gpu_count: 1,
  price_per_gpu_hour: 6.0,
  price_per_node_hour: 6.0,
  gpu_memory_gb: 179,
  cpu_count: 26,
  ram_gb: 354,
  disk_gb: 2442,
  reliability_score: 99.9,
  tier: 'secure',
  country: 'United States',
  country_code: 'US',
  city: 'Ashburn',
  rent_url: 'https://lium.io/browse-pods/80cc7015-a0cf-40b9-8c0a-ba1c1eec30f6',
};

const offerFields = [
  { key: 'gpu_model', label: 'GPU Model', required: false, helpText: 'Part of the GPU name, e.g. `H100`, `B200`, `4090`. Case-insensitive. Leave empty for any GPU.' },
  { key: 'max_price', label: 'Max Price per GPU-Hour (USD)', type: 'number', required: false, helpText: 'Only offers at or below this hourly price per GPU, in US dollars.' },
  { key: 'min_gpus', label: 'Minimum Free GPUs', type: 'integer', required: false, helpText: 'Only servers with at least this many GPUs free right now. Default 1.' },
];

const fetchOffers = async (z, bundle) => {
  const res = await z.request({ url: NODES_FEED });
  const model = (bundle.inputData.gpu_model || '').trim().toLowerCase();
  const maxPrice = present(bundle.inputData.max_price) ? Number(bundle.inputData.max_price) : null;
  const minGpus = Number(bundle.inputData.min_gpus || 1);
  return (res.data.nodes || [])
    .filter((n) => !model || String(n.gpu_model).toLowerCase().includes(model))
    .filter((n) => maxPrice === null || n.price_per_gpu_hour <= maxPrice)
    .filter((n) => (n.available_gpu_count ?? n.gpu_count) >= minGpus)
    .sort((a, b) => a.price_per_gpu_hour - b.price_per_gpu_hour);
};

const newOffer = {
  key: 'new_gpu_offer',
  noun: 'GPU Offer',
  display: {
    label: 'New GPU Offer',
    description:
      'Triggers when a matching GPU server is first observed after the Zap is enabled. Servers present at activation are not emitted, and previously seen servers do not trigger again.',
  },
  operation: { inputFields: offerFields, perform: fetchOffers, sample: offerSample },
};

const findOffers = {
  key: 'find_gpu_offers',
  noun: 'GPU Offer',
  display: {
    label: 'Find GPU Offers',
    description:
      'Finds GPU servers available to rent on Lium right now, cheapest first. Filter by GPU model, max price per GPU-hour and number of free GPUs.',
  },
  operation: { inputFields: offerFields, perform: fetchOffers, sample: offerSample },
};

// ---- Pods (your rented GPU servers) ----

const podSample = {
  id: 'f3b1c2d4-0000-4000-8000-000000000001',
  pod_id: 'f3b1c2d4-0000-4000-8000-000000000001',
  pod_name: 'my-training-run',
  status: 'RUNNING',
  ssh_connect_cmd: 'ssh root@203.0.113.7 -p 40022',
  price: 2.0,
  created_at: '2026-10-15T09:00:00Z',
};

const podStatusChanged = {
  key: 'pod_status_changed',
  noun: 'Pod',
  display: {
    label: 'New Pod',
    description: 'Triggers when you rent a new GPU pod on Lium. Status changes of an existing pod do not trigger.',
  },
  operation: {
    perform: async (z) => {
      const res = await z.request({ url: `${API}/pods` });
      // Zapier dedupes on `id`, so each pod triggers once; pod_id backs the Terminate Pod dropdown.
      return (res.data || []).map((p) => ({ ...p, id: p.id, pod_id: p.id }));
    },
    sample: podSample,
  },
};

// ---- Actions ----

// The node fields both rent paths can fill: a fresh rental's catalogue node and a replayed pod's executor. The per-GPU
// price is the charged $/h over the rented GPUs on both, not the catalogue's (which can differ from the charge).
const nodeOut = (e, pricePerHour, gpus) => ({ id: e.id, machine_name: e.machine_name, gpu_count: e.gpu_count,
  price_per_gpu: pricePerHour == null ? null : pricePerHour / gpus });

const rentPod = {
  key: 'rent_gpu_pod',
  noun: 'Pod',
  display: {
    label: 'Rent GPU Pod',
    description:
      'Rents the cheapest available Lium GPU server that matches the GPU model, GPU count and max price, and starts a pod you can SSH into. Billed per second at the hourly price until you terminate it.',
  },
  operation: {
    inputFields: [
      { key: 'gpu_type', label: 'GPU Model', required: true, helpText: 'GPU to rent, e.g. `H100`, `H200`, `B200`, `A100`, `RTX4090`.' },
      { key: 'gpu_count', label: 'GPU Count', type: 'integer', required: false, default: '1', helpText: 'Number of GPUs. Default 1.' },
      { key: 'max_price_per_gpu_hour', label: 'Max Price per GPU-Hour (USD)', type: 'number', required: false, helpText: 'Do not rent above this hourly price per GPU.' },
      { key: 'ssh_public_key', label: 'SSH Public Key', required: true, helpText: 'Your SSH public key (the line starting `ssh-ed25519` or `ssh-rsa`). It is installed on the pod so you can log in.' },
      { key: 'pod_name', label: 'Pod Name', required: false, helpText: 'A name prefix for the pod. Default `zap`. The action appends a code derived from the Idempotency Key and the rental (e.g. `zap-1a2b3c4d5e6f`) so a late replay can find the pod it already rented.' },
      {
        key: 'idempotency_key',
        label: 'Idempotency Key',
        required: true,
        helpText:
          'A value that stays the same if Zapier replays this step, so a retry returns the first pod instead of renting a second one. Map a stable value from the trigger step, e.g. the trigger item\'s ID; do not use a timestamp or a random value. Use a different key for each pod, e.g. `{{id}}-2` for a second Rent step. Lium remembers a key for 24 hours; after that, a replay still finds and returns the pod this key rented as long as that pod exists under the same name, marked `replayed: true`. Replaying a rent by hand more than 24 hours later, after renaming or deleting its pod, rents a new pod.',
      },
      { key: 'template_id', label: 'Template ID', required: false, helpText: 'Optional Lium template (docker image) ID. Empty: the server\'s recommended PyTorch image.' },
    ],
    perform: async (z, bundle) => {
      const d = bundle.inputData;
      const key = String(d.idempotency_key || '').trim();
      if (!key) throw new errors.Error('Idempotency Key is required: map a stable value from the trigger step.');
      const keyHash = createHash('sha256').update(key).digest('hex');
      const gpuCount = Number(d.gpu_count || 1);
      const sshKey = d.ssh_public_key.trim();
      // The name carries the key and the rental, so a replay after the backend's 24 h key window still finds the pod
      // and the same key on another rental does not (the max price is left out: editing the cap keeps replays matching).
      const nameCode = createHash('sha256')
        .update(JSON.stringify([key, d.gpu_type, gpuCount, sshKey, d.template_id || null])).digest('hex').slice(0, 12);
      const body = {
        gpu_type: d.gpu_type,
        gpu_count: gpuCount,
        pod_name: `${d.pod_name || 'zap'}-${nameCode}`,
        user_public_key: [sshKey],
        // said, so the pod holds the SSH port only: an unsaid rent also holds a Jupyter port (DAH-3698)
        enable_jupyter: false,
      };
      if (present(d.max_price_per_gpu_hour)) {
        const cap = Number(d.max_price_per_gpu_hour);
        if (!Number.isFinite(cap) || cap <= 0) throw new errors.Error('Max Price per GPU-Hour must be a number above 0.');
        body.max_price_per_gpu_hour = cap;
      }
      if (d.template_id) body.template_id = d.template_id;
      const existing = ((await z.request({ url: `${API}/pods` })).data || []).find((p) => p.pod_name === body.pod_name);
      if (existing) {
        // same output fields as a fresh rental; /pods carries the charged total $/h as `price`
        const e = existing.executor || {};
        return { ...existing, id: existing.id, pod_id: existing.id, replayed: true, price_per_hour: existing.price,
          template_id: (existing.template || {}).id,
          node: nodeOut(e, existing.price, gpuCount) };
      }
      // The backend accepts 1-255 of [A-Za-z0-9-_.:]; any other value is hashed, which is just as replay-stable.
      const headers = { 'Idempotency-Key': /^[A-Za-z0-9._:-]{1,255}$/.test(key) ? key : keyHash };
      const res = await z.request({ method: 'POST', url: `${API}/executors/rent-by-spec`, headers, body, skipThrowForStatus: true });
      if (res.status === 409 && /idempotency_(in_progress|outcome_unknown)/.test(JSON.stringify(res.data))) {
        throw new errors.Error(
          'Lium is still processing, or could not confirm, an earlier rental with this Idempotency Key. It may already have rented a pod: check your pods at https://lium.io before retrying. Retrying with the same key is refused; use a new key only once you know no pod was rented.',
          'idempotency_conflict',
          409,
        );
      }
      if (res.throwForStatus) res.throwForStatus();
      const r = res.data;
      return { id: r.pod_id, pod_id: r.pod_id, pod_name: body.pod_name, replayed: false, status: 'PENDING', price_per_hour: r.price_per_hour, template_id: r.template_id, node: nodeOut(r.selected_executor || {}, r.price_per_hour, gpuCount) };
    },
    sample: { id: podSample.pod_id, pod_id: podSample.pod_id, pod_name: 'zap-1a2b3c4d5e6f', replayed: false, status: 'PENDING', price_per_hour: 2.0 },
  },
};

const terminatePod = {
  key: 'terminate_pod',
  noun: 'Pod',
  display: {
    label: 'Terminate Pod',
    description:
      'Requests deletion of a Lium GPU pod. Teardown may complete asynchronously; data on the pod is lost when deletion completes.',
  },
  operation: {
    inputFields: [
      { key: 'pod_id', label: 'Pod ID', required: true, dynamic: 'pod_status_changed.pod_id.pod_name', helpText: 'The pod to terminate (its ID from Rent GPU Pod or the pod trigger).' },
    ],
    perform: async (z, bundle) => {
      const id = encodeURIComponent(bundle.inputData.pod_id);
      const res = await z.request({ method: 'DELETE', url: `${API}/pods/${id}` });
      // HTTP 200 means deletion was accepted ("started" / "in progress"), not that teardown finished.
      return { id: bundle.inputData.pod_id, deletion_requested: true, response: res.data };
    },
    sample: { id: podSample.pod_id, deletion_requested: true },
  },
};

module.exports = {
  version,
  platformVersion,
  authentication,
  flags: { cleanInputData: false },
  beforeRequest: [addApiKey],
  triggers: { [newOffer.key]: newOffer, [podStatusChanged.key]: podStatusChanged },
  searches: { [findOffers.key]: findOffers },
  creates: { [rentPod.key]: rentPod, [terminatePod.key]: terminatePod },
};
