"""A node the backend has no default image for gets Lium's own PyTorch template, not a community one
whose date tag outranks a version (DAH-5795: a CUDA 12.4 node rented a ComfyUI image with no torch)."""

import pytest

from lium.sdk import Config, Lium


def _row(id, tag, user_id=None, image="daturaai/pytorch"):
    return {"id": id, "name": id, "docker_image": image, "docker_image_tag": tag, "category": "PYTORCH",
            "user_id": user_id}


TEMPLATES = [
    _row("community", "26022026", user_id="u1", image="ls250824/run-comfyui-wan2"),
    _row("user-copy", "2.12.0-py3.12-cuda12.4-devel-ubuntu24.04-dind-lium1", user_id="u2"),
    _row("cu130", "2.12.0-py3.12-cuda13.0.2-devel-ubuntu24.04-dind-lium1"),
    _row("cu128", "2.11.0-py3.12-cuda12.8-devel-ubuntu24.04-dind-lium1"),
    _row("cu126", "2.12.0-py3.12-cuda12.6-devel-ubuntu24.04-dind-lium1"),
]


@pytest.fixture
def lium(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = Lium(Config(api_key="k"))

    class _Resp:
        def json(self):
            return TEMPLATES

    monkeypatch.setattr(client, "_request", lambda method, endpoint, **kw: _Resp())
    return client


@pytest.mark.parametrize("max_cuda, expected", [(13.1, "cu130"), (12.8, "cu128"), (12.6, "cu126"), (12.4, "cu126"), (None, "cu130")])
def test_fallback_is_the_newest_official_pytorch_the_driver_runs(lium, max_cuda, expected):
    assert lium._select_fallback_template(max_cuda).id == expected
