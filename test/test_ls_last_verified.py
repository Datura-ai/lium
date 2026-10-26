"""``ExecutorInfo.last_verified_at``: the API's last passed validator check, carried as sent."""

from lium.sdk import Config, Lium


def _info(**extra):
    row = {"id": "e1", "machine_name": "NVIDIA H100 80GB HBM3", "price_per_gpu": 2.0, "gpu_count": 8, **extra}
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(row)


def test_ls_carries_the_last_verified_time_the_api_sends():
    assert _info(last_verified_at="2026-10-10T08:00:00Z").last_verified_at == "2026-10-10T08:00:00Z"


def test_a_node_never_verified_or_from_an_older_api_has_no_last_verified_time():
    assert _info(last_verified_at=None).last_verified_at is None
    assert _info().last_verified_at is None
