"""GET /pods/{id}'s `template` (TemplateBaseResponse) has no docker_credential_id, supports_docker,
supports_volume_encryption or health_check_command, and PUT /templates/{id} writes every field it is given,
so edit() builds its body from the template row itself."""

from lium.sdk import Config, Lium

POD_TEMPLATE = {  # what GET /pods/{id} carries: TemplateBaseResponse
    "id": "t1", "name": "Pytorch (Cuda + DinD)", "description": None, "docker_image": "daturaai/pytorch",
    "docker_image_tag": "2.12.0-dind", "category": "PYTORCH", "docker_image_digest": None,
    "volumes": ["/workspace"], "environment": {}, "entrypoint": "", "internal_ports": [22],
    "is_private": True, "startup_commands": "",
}
TEMPLATE_ROW = {**POD_TEMPLATE, "docker_credential_id": "cred-1", "supports_docker": True,
                "supports_volume_encryption": True, "health_check_command": "true", "is_temporary": True}


def test_edit_sends_back_the_fields_the_pod_listing_does_not_carry(monkeypatch, tmp_path):
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
            return _Resp(TEMPLATE_ROW)
        if method == "PUT":
            puts.append(kw["json"])
            return _Resp(kw["json"])
        raise AssertionError((method, endpoint))

    monkeypatch.setattr(lium, "_request", fake_request)

    lium.edit("p1", startup_commands="python serve.py")

    body = puts[0]
    assert body["startup_commands"] == "python serve.py"
    assert body.get("docker_credential_id") == "cred-1"
    assert body.get("supports_docker") is True
    assert body.get("supports_volume_encryption") is True
