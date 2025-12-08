#!/usr/bin/env python3
"""Quick example: Run vLLM with proper entrypoint."""

from lium.sdk import Lium

lium = Lium()

# volume to cache your huggingface models between pods
template = lium.create_template(
    name="vllm-llama32-1b",
    docker_image="vllm/vllm-openai",
    ports=[22, 8000],
    environment={
        "HF_HOME": "/root/.cache/huggingface",
        "HF_TOKEN": "****",
        "MODEL_NAME": "HuggingFaceTB/smollm-135m"
    },
    entrypoint="sh",
    # piping to a log file is never a bad idea
    start_command="-c 'vllm serve $MODEL_NAME > vllm.log'",
)

executors = lium.ls(gpu_type="A100")
assert len(executors) > 0, "No executors found"

first = executors[0]
pod = lium.up(executor_id=first.id, template_id=template.id)
ready = lium.wait_ready(pod["id"])

if ready:
    pod = lium.pod(pod_id=pod['id'])

    if "8000" in pod["ports_mapping"]:
        port = pod["ports_mapping"]["8000"]
        host = pod["executor"]["executor_ip_address"]
        print(f"\nvLLM is loading! You can now make requests to make sure it's ready:")
        print(f"http://{host}:{port}/v1/models")

"""
You can later change the model name as you like:

lium.edit(pods[0].id, environment={'MODEL_NAME': "meta-llama/Llama-3.2-1B-Instruct"})
"""
