"""DAH-5995: the notes a provider shows renters reach ExecutorInfo."""

from lium.cli.ls.display import compact_executor
from lium.sdk import Config, Lium


def _map(**extra):
    executor = {
        "id": "n1",
        "machine_name": "NVIDIA H200 8x",
        "executor_ip_address": "203.0.113.4",
        "price_per_gpu": 3.25,
        "status": "available",
        "location": {},
        "specs": {"gpu": {"count": 8, "details": [{"name": "H200", "capacity": 143771}]}},
        **extra,
    }
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor)


def test_the_notes_are_read_off_the_listing():
    assert _map(provider_notes=["May reboot occasionally"]).provider_notes == ["May reboot occasionally"]
    assert _map(provider_notes=[]).provider_notes == []


def test_an_older_api_without_the_field_reads_none():
    assert _map().provider_notes is None


def test_lium_ls_json_carries_the_notes():
    assert compact_executor(_map(provider_notes=["May reboot occasionally"]), False, 1)["provider_notes"] == [
        "May reboot occasionally"
    ]
    assert compact_executor(_map(), False, 1)["provider_notes"] is None
