"""_dict_to_executor_info must not crash or misprice on sparse marketplace records,
and `lium config set template.default_id` must call an SDK method that exists."""
from types import SimpleNamespace

from lium.sdk import Config, Lium
from lium.cli.settings import ConfigManager


def _client():
    return Lium(Config(api_key="test"))


def _record(**overrides):
    base = {
        "id": "e1",
        "machine_name": "NVIDIA H100 80GB HBM3",
        "specs": {"gpu": {"count": 8, "details": [{"name": "NVIDIA H100 80GB HBM3"}] * 8}},
        "price_per_gpu": 2.0,
        "location": {},
    }
    base.update(overrides)
    return base


def test_empty_machine_name_falls_back_to_gpu_details():
    info = _client()._dict_to_executor_info(_record(machine_name=""))
    assert info.gpu_type == "H100"
    assert info.gpu_count == 8


def test_missing_machine_name_key_does_not_raise():
    rec = _record()
    del rec["machine_name"]
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_type == "H100"


def test_top_level_gpu_count_is_the_billed_one():
    """The record's own gpu_count (the Executor column the rent path bills on) wins
    over a stale count inside the scraped specs."""
    rec = _record(gpu_count=8, specs={"gpu": {"count": 4, "details": [{"name": "NVIDIA H100 80GB HBM3"}] * 4}})
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_count == 8
    assert info.price_per_hour == 16.0


def test_string_gpu_count_is_parsed_not_multiplied_as_text():
    """Some payloads send counts as strings (the /pods rows do); "8" must price as 8, not raise."""
    rec = _record(gpu_count="8", specs={"gpu": {"count": "8", "details": [{"name": "NVIDIA H100 80GB HBM3"}] * 8}})
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_count == 8
    assert info.price_per_hour == 16.0


def test_unparseable_gpu_count_falls_through_to_specs():
    rec = _record(gpu_count="eight", specs={"gpu": {"count": 4, "details": [{"name": "NVIDIA H100 80GB HBM3"}] * 4}})
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_count == 4


def test_whitespace_only_machine_name_falls_back_to_gpu_details():
    info = _client()._dict_to_executor_info(_record(machine_name="   "))
    assert info.gpu_type == "H100"


def test_missing_count_uses_number_of_listed_gpus_not_one():
    rec = _record(specs={"gpu": {"details": [{"name": "NVIDIA H200"}] * 4}})
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_count == 4
    assert info.price_per_hour == 8.0  # 4 × $2, not 1 × $2


def test_no_gpu_specs_at_all_defaults_to_one_gpu():
    rec = _record(specs={}, machine_name="NVIDIA RTX 4090")
    info = _client()._dict_to_executor_info(rec)
    assert info.gpu_count == 1
    assert info.gpu_type == "RTX4090"


def test_null_specs_and_null_machine_name_do_not_raise():
    info = _client()._dict_to_executor_info(_record(specs=None, machine_name=None))
    assert info.gpu_count == 1
    assert info.gpu_type == "Unknown"


def test_select_template_uses_existing_sdk_method(monkeypatch, tmp_path):
    # The method the CLI calls must be the one the real SDK class has (the bug was `list_templates`).
    assert callable(getattr(Lium, "templates", None))
    assert not hasattr(Lium, "list_templates")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_API_KEY", "test")
    calls = []

    class FakeLium:
        def __init__(self, *a, **k):
            pass

        def templates(self, *a, **k):
            calls.append("templates")
            return [SimpleNamespace(id="tpl-1", name="PyTorch CUDA")]

    monkeypatch.setattr("lium.sdk.Lium", FakeLium)
    monkeypatch.setattr("lium.cli.settings.Prompt.ask", lambda *a, **k: "1")

    assert ConfigManager()._select_template() == "tpl-1"
    assert calls == ["templates"]
