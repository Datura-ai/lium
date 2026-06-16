from lium.cli.up.actions import CreateEphemeralTemplateAction
from lium.sdk import Config, Lium, Template


class _Response:
    def json(self):
        return {
            "id": "template-123",
            "name": "ephemeral-test",
            "docker_image": "alpine",
            "docker_image_tag": "latest",
            "category": "DOCKER",
            "status": "VERIFY_SUCCESS",
        }


def test_create_template_forwards_one_time_and_temporary_flags(monkeypatch):
    captured = {}

    def fake_request(self, method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        return _Response()

    monkeypatch.setattr(Lium, "_request", fake_request)

    client = Lium(Config(api_key="test"))
    client.create_template(
        name="ephemeral-test",
        docker_image="alpine",
        one_time_template=True,
        is_temporary=False,
    )

    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/templates"
    assert captured["json"]["one_time_template"] is True
    assert captured["json"]["is_temporary"] is False


def test_create_ephemeral_template_action_marks_template_one_time():
    captured = {}

    class FakeLium:
        def create_template(self, **kwargs):
            captured.update(kwargs)
            return Template(
                id="template-123",
                huid="brave-fox-12",
                name=kwargs["name"],
                docker_image=kwargs["docker_image"],
                docker_image_tag=kwargs["docker_image_tag"],
                category="DOCKER",
                status="VERIFY_SUCCESS",
            )

    result = CreateEphemeralTemplateAction().execute(
        {
            "lium": FakeLium(),
            "image": "alpine:latest",
            "env": {"A": "1"},
            "entrypoint": "/bin/sh",
            "cmd": "sleep infinity",
            "ports": [22],
        }
    )

    assert result.ok is True
    assert captured["name"].startswith("ephemeral-")
    assert captured["is_private"] is True
    assert captured["one_time_template"] is True
