Getting Started
===============

Installation
------------

The SDK ships with the `lium.io` package on PyPI:

.. code-block:: bash

   pip install lium.io

Managed binary installs are also available for macOS and Linux on amd64 and arm64:

.. code-block:: bash

   curl -fsSL https://github.com/Datura-ai/lium-cli/releases/latest/download/install.sh | bash

Fresh binary installs create ``~/.lium/bin/lium`` as a managed symlink pointing at a
versioned binary under ``~/.lium/versions/<version>/lium``.

Authentication requires an API key stored in ``~/.lium/config.ini`` or exported as
``LIUM_API_KEY``. The CLI bootstraps this for you: ``lium init`` in a browser, or
``lium init --api-key <key>`` on a machine without one (agents, CI, containers).

The CLI sends no telemetry unless you opt in with ``lium config set telemetry.enabled true``
(or ``LIUM_TELEMETRY=1``); then an unexpected error is reported with the command name, the stack
trace, the exception message with home paths, e-mails, API keys and the values you passed on the
command line cut out, plus the CLI version, Python, OS and API host — never arguments, local
variables or your account. See the README's "Crash reporting" section.

Example
-------

The ``@lium.machine`` decorator is the easiest way to offload work to a GPU pod.
``machine`` is ``"<count>x<gpu>"`` or ``"<gpu>"`` (``"1xH200"``, ``"A100"``, ``"2xRTX4090"``;
the count defaults to 1; the GPU is named as ``lium ls --gpu`` takes it and matched whole, so
``"A100"`` never rents an RTX A1000) and the cheapest matching node is rented. ``timeout=`` (default one
hour) bounds the run; the pod is scheduled for removal at ``timeout + 15 min`` — armed when the pod
is rented and again once setup is done, so the run itself gets the full window — regardless of
what happens to the caller.

.. code-block:: python

   import lium

   @lium.machine(machine="A100", requirements=["torch", "transformers", "accelerate"])
   def infer(prompt: str) -> str:
       from transformers import AutoTokenizer, AutoModelForCausalLM
       tokenizer = AutoTokenizer.from_pretrained("sshleifer/tiny-gpt2")
       model = AutoModelForCausalLM.from_pretrained("sshleifer/tiny-gpt2", device_map="cuda")
       tokens = tokenizer(prompt, return_tensors="pt").to("cuda")
       out = model.generate(**tokens, max_new_tokens=50)
       return tokenizer.decode(out[0], skip_special_tokens=True)

   print(infer("Who discovered penicillin?"))

``keep_warm=300`` keeps the pod five minutes for the next call or the next run of the
script; ``infer.map(prompts)`` runs every item on one pod; ``infer.local(...)`` runs the
function in this process (``local=True`` / ``LIUM_MACHINE_LOCAL=1`` does so for every
call); ``infer.close()`` removes a warm pod. Arguments travel as a pickle; the
result comes back as a JSON envelope plus an ``.npz`` sidecar for numpy arrays, read with
``allow_pickle=False`` — nothing the pod writes is unpickled on your machine. What round-trips:
``None``/``bool``/``int``/``float``/``str``/``bytes``, ``list``/``tuple``/``set``/``frozenset``/``dict``
of those, ``datetime``/``date``/``time``/``timedelta``, ``Decimal``, ``pathlib.Path``, ``uuid.UUID``,
``numpy.ndarray`` (any dtype without Python objects) and numpy scalars; anything else is a
``lium.ResultEncodingError`` on the pod naming the type (return ``.tolist()``, ``dict(x)``,
``x.value`` instead). Only the function's own ``def`` is sent, so import inside it. A remote exception is re-raised with
its type when that type is a builtin (``except ValueError`` works; other types arrive as
``lium.RemoteExecutionError`` with the name), with ``lium.RemoteExecutionError`` (remote traceback, exit code,
output) as its cause. Progress lines go to stderr (``quiet=True`` to silence them).

Direct SDK usage follows the same pattern:

.. code-block:: python

   from lium.sdk import Lium

   lium = Lium()
   node = lium.ls(gpu_type="A100")[0]
   pod = lium.up(executor_id=node.id, name="demo")
   ready = lium.wait_ready(pod, timeout=600)
   print(lium.exec(ready, command="nvidia-smi")["stdout"])

Most pod-level SDK calls (`exec`, `down`, `backup_*`, etc.) expect a :class:`lium.sdk.PodInfo`
instance. Use `lium.ps()` or `lium.wait_ready()` to obtain the dataclass before passing the pod to
other methods.

vLLM Deployment
~~~~~~~~~~~~~~~

A more complete example showing how to deploy vLLM on the Lium platform:

.. literalinclude:: ../examples/quick_vllm.py
   :language: python
   :linenos:

First hour on a Lium pod
------------------------

A short checklist for getting a freshly rented pod into a productive state. Everything below
runs inside the pod (``lium ssh <pod>`` or ``lium exec <pod> "..."``).

**Always set a lifetime.** A pod bills until it is removed. Pass ``--ttl`` (or ``--until``) on
every ``lium up`` so a forgotten pod terminates on its own:

.. code-block:: bash

   lium up --gpu H200 -c 8 --ttl 6h --yes

**Know where the volume is.** A pod has one local volume, mounted where its template says —
``/root`` on the standard templates (in the SDK it is ``pod.volume_path``). That path is the only
one ``lium bk`` can back up (the backend rejects paths outside it) and the one volume encryption
covers. A Volume you attach with ``--volume`` is mounted under ``/mnt``. Anything else on the pod —
``/workspace``, ``/tmp`` — is plain container filesystem: not encrypted, and ``lium bk`` cannot
back it up. Keep checkpoints, datasets and the Hugging Face cache on the volume, and point the cache
there before the first download:

.. code-block:: bash

   mkdir -p /root/hf /root/logs
   export HF_HOME=/root/hf
   export HF_HUB_ENABLE_HF_TRANSFER=1   # faster downloads; needs `pip install hf_transfer`

With encryption on (the default) the volume is a FUSE mount, so very large sequential reads are
slower there than from the container filesystem. A cache you can re-download is the one thing worth
trading durability for speed on; anything you would want back stays on the volume or on an attached
Volume under ``/mnt``.

**Installing Python packages on Ubuntu 24.04 images.** System Python is externally managed
(PEP 668), so a bare ``pip install`` fails. Either create a virtual environment or opt out
explicitly:

.. code-block:: bash

   python -m venv /root/venv && source /root/venv/bin/activate
   # or
   export PIP_BREAK_SYSTEM_PACKAGES=1

**Blackwell GPUs need a recent CUDA build of PyTorch.** B200, B300, RTX PRO 6000 and RTX 5090
(compute capability 10.x / 12.x) are not supported by wheels built for CUDA 12.4 or older. Install
a cu128 or newer build, for example:

.. code-block:: bash

   pip install torch --index-url https://download.pytorch.org/whl/cu130

FlashAttention-3 targets Hopper (H100/H200) only. On Blackwell use FlashAttention-4 or the cuDNN
attention backend (``torch.nn.attention.sdpa_kernel``) instead.

**Common system packages.** Minimal images may lack tools that training and data scripts assume:

.. code-block:: bash

   apt-get update && apt-get install -y ffmpeg rsync

**Background jobs.** A plain ``nohup cmd &`` started through ``lium exec`` keeps the SSH session
open and can die with it. Detach the process from the session and close its stdin (the log
directory was created above):

.. code-block:: bash

   nohup setsid python train.py > /root/logs/train.log 2>&1 < /dev/null &

**Check that the GPUs are being used.** Sample utilisation to a CSV while a job runs, then look at
it with any tool:

.. code-block:: bash

   nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used \
     --format=csv -l 5 > /root/logs/gpu.csv &

``nvidia-smi -L`` lists the GPUs the pod actually has; compare the count with the one
``lium ps`` shows for the pod.
