"""GET /pods/{id}'s `template` (TemplateBaseResponse) has no docker_credential_id, supports_docker,
supports_volume_encryption or health_check_command, and PUT /templates/{id} writes every field it is given,
so edit() builds its body from the template row itself."""

import pytest

from lium.sdk import Config, Lium

POD_TEMPLATE = {  # what GET /pods/{id} carries: TemplateBaseResponse
    "id": "t1", "name": "Pytorch (Cuda + DinD)", "description": None, "docker_image": "daturaai/pytorch",
    "docker_image_tag": "2.12.0-dind", "category": "PYTORCH", "docker_image_digest": None,
    "volumes": ["/workspace"], "environment": {}, "entrypoint": "", "internal_ports": [22],
    "is_private": True, "startup_commands": "",
}
TEMPLATE_ROW = {**POD_TEMPLATE, "docker_credential_id": "cred-1", "supports_docker": True,
                "supports_volume_encryption": True, "health_check_command": "true", "is_temporary": True}


def _edit(monkeypatch, tmp_path, template_row, **kwargs):
    monkeypatch.setenv("HOME", str(tmp_path))
    lium = Lium(Config(api_key="k"))
    puts = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_request(method, endpoint, **kw):
        if method == "GET" and endpoint == "/pods/p1":
            return _Resp({"id": "p1", "template": POD_TEMPLATE})
        if method == "GET" and endpoint == "/templates/t1":
            return _Resp(template_row)
        if method == "PUT":
            puts.append(kw["json"])
            return _Resp(kw["json"])
        raise AssertionError((method, endpoint))

    monkeypatch.setattr(lium, "_request", fake_request)

    lium.edit("p1", **kwargs)
    return puts[0]


def test_edit_sends_back_the_fields_the_pod_listing_does_not_carry(monkeypatch, tmp_path):
    body = _edit(monkeypatch, tmp_path, TEMPLATE_ROW, startup_commands="python serve.py")

    assert body["startup_commands"] == "python serve.py"
    assert body.get("docker_credential_id") == "cred-1"
    assert body.get("supports_docker") is True
    assert body.get("supports_volume_encryption") is True
    assert body.get("health_check_command") == "true"


def test_edit_leaves_out_the_template_row_null_columns(monkeypatch, tmp_path):
    row = {**TEMPLATE_ROW, "volumes": None, "environment": None, "entrypoint": None, "docker_image_digest": None}

    body = _edit(monkeypatch, tmp_path, row, startup_commands="python serve.py")

    assert not {"volumes", "environment", "entrypoint", "docker_image_digest", "description"} & body.keys()
    assert body["startup_commands"] == "python serve.py"


@pytest.mark.parametrize("is_temporary", [True, False, None])
@pytest.mark.parametrize("one_time_template", [None, False])
@pytest.mark.parametrize("kwargs", [{}, {"is_temporary": True, "one_time_template": False}])
def test_edit_omits_temporary_flags_for_public_templates(monkeypatch, tmp_path, is_temporary, one_time_template, kwargs):
    row = {**TEMPLATE_ROW, "is_temporary": is_temporary, "is_private": False, "one_time_template": one_time_template}

    body = _edit(monkeypatch, tmp_path, row, environment={"DEBUG": "1"}, **kwargs)

    assert not {"is_temporary", "one_time_template"} & body.keys()
    assert body["environment"] == {"DEBUG": "1"}


def test_edit_preserves_one_time_template_cleanup(monkeypatch, tmp_path):
    row = {**TEMPLATE_ROW, "one_time_template": True, "is_private": True}

    body = _edit(monkeypatch, tmp_path, row)

    assert body["one_time_template"] is True
