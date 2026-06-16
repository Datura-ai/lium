from lium.sdk import Config, Lium


def test_client_sets_version_header(monkeypatch):
    monkeypatch.setattr("lium.sdk.client._get_client_version", lambda: "1.2.3")

    client = Lium(Config(api_key="test"), source="cli")

    assert client.headers["X-API-KEY"] == "test"
    assert client.headers["X-Source"] == "cli"
    assert client.headers["X-Lium-Client-Version"] == "1.2.3"
