"""up(image=...) checks for an SSH key before it creates the private one-time template, so a call
refused for want of a key leaves no `ephemeral-<hash>` template on the account."""

import pytest

from lium.sdk import Config, ExecutorInfo, Lium


def test_up_with_image_and_no_ssh_key_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    lium = Lium(Config(api_key="k", ssh_key_path=None))
    node = ExecutorInfo(id="e1", ip="", huid="h", machine_name="1x H100", gpu_type="H100", gpu_count=1,
                        price_per_hour=1.0, price_per_gpu=1.0, location={}, specs={}, status="", docker_in_docker=False)
    monkeypatch.setattr(lium, "get_executor", lambda _: node)
    sent = []

    class _Resp:
        def json(self):
            return {"id": "tpl-1"}

    monkeypatch.setattr(lium, "_request", lambda method, endpoint, **kw: sent.append((method, endpoint)) or _Resp())

    with pytest.raises(ValueError, match="No SSH keys"):
        lium.up(executor_id="e1", image="vllm/vllm-openai:latest")

    assert sent == []
