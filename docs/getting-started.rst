Getting Started
===============

Installation
------------

The SDK ships with the `lium.io` package on PyPI:

.. code-block:: bash

   pip install lium.io

Authentication requires an API key stored in ``~/.lium/config.ini`` or exported as
``LIUM_API_KEY``. The CLI (`lium init`) can bootstrap this for you.

Example
-------

The ``@lium.machine`` decorator is the easiest way to offload work to a GPU pod.

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

Direct SDK usage follows the same pattern:

.. code-block:: python

   from lium.sdk import Lium

   lium = Lium()
   executor = lium.ls(gpu_type="A100")[0]
   pod = lium.up(executor=executor.id, name="demo")
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
