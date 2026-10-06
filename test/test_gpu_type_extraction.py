"""GPU type extraction / shorthand resolution (RTX PRO 6000 Blackwell regression)."""
import pytest

from lium.sdk import Config, Lium
from lium.sdk.utils import extract_gpu_type, normalize_gpu_short


@pytest.mark.parametrize(
    "machine_name,expected",
    [
        ("NVIDIA H100 80GB HBM3", "H100"),
        ("NVIDIA H100 PCIe", "H100"),
        ("NVIDIA H200", "H200"),
        ("NVIDIA B200", "B200"),
        ("NVIDIA B300 SXM6 AC", "B300"),
        ("NVIDIA GeForce RTX 4090", "RTX4090"),
        ("NVIDIA GeForce RTX 5090", "RTX5090"),
        ("NVIDIA RTX 6000 Ada Generation", "RTX6000"),
        ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "RTXPRO6000"),
        ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "RTXPRO6000"),
        # the 84 GB "6000D" is its own SKU: the suffix stays, so `--gpu pro6000` does not rent it
        ("NVIDIA RTX PRO 6000D Blackwell Workstation Edition", "RTXPRO6000D"),
        ("NVIDIA RTX A6000", "A6000"),
        ("NVIDIA A100-SXM4-80GB", "A100"),
        ("NVIDIA L40S", "L40S"),
        ("NVIDIA L40", "L40"),
        ("NVIDIA CMP 170HX", "170HX"),
        ("", "Unknown"),
    ],
)
def test_extract_gpu_type(machine_name, expected):
    assert extract_gpu_type(machine_name) == expected


def test_rtx_pro_6000_is_not_reported_as_edition():
    # Regression: the marketplace showed 25 nodes as "1×Edition" because "PRO" broke the RTX match.
    for name in (
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
    ):
        assert extract_gpu_type(name) != "Edition"


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("RTXPRO6000", "RTXPRO6000"),
        ("rtxpro6000", "RTXPRO6000"),
        ("RTX PRO 6000", "RTXPRO6000"),
        ("rtx-pro-6000", "RTXPRO6000"),
        ("PRO6000", "RTXPRO6000"),
        ("pro 6000", "RTXPRO6000"),
        ("RTX6000PRO", "RTXPRO6000"),
        ("pro6000d", "RTXPRO6000D"),
        ("h100", "H100"),
        ("RTX 4090", "RTX4090"),
    ],
)
def test_normalize_gpu_short(typed, expected):
    assert normalize_gpu_short(typed) == expected


def test_normalized_short_matches_extracted_type():
    # The `--gpu` filter normalises both sides, so every alias must land on the extracted type.
    machine = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
    for typed in ("RTXPRO6000", "pro6000", "RTX PRO 6000"):
        assert normalize_gpu_short(typed) == normalize_gpu_short(extract_gpu_type(machine))
    assert normalize_gpu_short("RTX6000") == normalize_gpu_short(
        extract_gpu_type("NVIDIA RTX 6000 Ada Generation")
    )
    assert normalize_gpu_short("RTX6000") != normalize_gpu_short(extract_gpu_type(machine))
    # `--gpu pro6000` must not pick the 84 GB 6000D, and `pro6000d` must not pick the 96 GB card
    six_d = extract_gpu_type("NVIDIA RTX PRO 6000D Blackwell Workstation Edition")
    assert normalize_gpu_short("pro6000") != normalize_gpu_short(six_d)
    assert normalize_gpu_short("pro6000d") == normalize_gpu_short(six_d)
    assert normalize_gpu_short("pro6000d") != normalize_gpu_short(extract_gpu_type(machine))


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("ti", "NVIDIA GeForce GTX 1080 Ti"),
        ("TI", "NVIDIA GeForce GTX 1080 Ti"),
        ("xp", "NVIDIA TITAN Xp"),
        ("rtx4090", "NVIDIA GeForce RTX 4090"),
        ("pro6000", "NVIDIA RTX PRO 6000 Blackwell Server Edition"),
    ],
)
def test_resolve_machine_name_matches_fall_through_names_in_any_case(monkeypatch, typed, expected):
    # Names with no pattern hit keep their casing ("Ti", "Xp"); the `--gpu` filter
    # must still match them case-insensitively, as it did before this change.
    machines = [
        {"name": "NVIDIA GeForce GTX 1080 Ti"},
        {"name": "NVIDIA TITAN Xp"},
        {"name": "NVIDIA GeForce RTX 4090"},
        {"name": "NVIDIA RTX PRO 6000 Blackwell Server Edition"},
    ]

    class Response:
        def json(self):
            return machines

    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "_request", lambda method, endpoint, **kwargs: Response())

    assert client._resolve_machine_name(typed) == expected
