'use strict';

// Lium (lium.io) is a GPU cloud: rent GPU servers (H100, B200, RTX 4090 ...) by the hour.
// This app wraps Lium's REST API (https://lium.io/api), the same API the `lium` CLI and SDK use.
const { version } = require('./package.json');
const { version: platformVersion, errors } = require('zapier-platform-core');

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

// Hidden, and never rents: Zapier publishes no integration that makes financial transactions, and an action cannot be
// removed (C011) while old Zaps reference it.
const rentPod = {
  key: 'rent_gpu_pod',
  noun: 'Pod',
  display: {
    label: 'Rent GPU Pod (removed)',
    description: 'Removed: renting is not available from Zapier. Rent GPU pods on lium.io, the lium CLI or the API.',
    hidden: true,
  },
  operation: {
    // the 1.0.4 fields stay so Zapier can migrate Zaps that use them (C002)
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
    perform: async () => {
      throw new errors.Error('Renting is not available from Zapier. Rent GPU pods on lium.io, the lium CLI or the API.', 'RentRemoved', 400);
    },
    sample: { id: 'removed' },
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
      { key: 'pod_id', label: 'Pod ID', required: true, dynamic: 'pod_status_changed.pod_id.pod_name', helpText: 'The pod to terminate (its ID from the New Pod trigger).' },
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
