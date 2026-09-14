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
