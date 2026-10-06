"""`lium templates`: which GPU generation an image runs on.

A cu126 template on a B200 fails at the first CUDA call. The template record
does not say "Blackwell", but its tag says which CUDA it was built with, and
that is enough to label every template and to filter on `--arch`.
"""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.templates import arch
from lium.cli.templates import command as templates_module
from lium.sdk import Template
from lium.sdk.utils import generate_huid


def _template(tid, name, image, tag, category="PYTORCH", status="VERIFY_SUCCESS"):
    return Template(id=tid, huid=generate_huid(tid), name=name, docker_image=image,
                    docker_image_tag=tag, category=category, status=status)


CU130 = _template("t-130", "Pytorch DinD", "daturaai/pytorch", "2.12.0-py3.12-cuda13.0.2-devel-ubuntu24.04-dind")
CU128 = _template("t-128", "PyTorch 2.7", "pytorch/pytorch", "2.7.1-cuda12.8-cudnn9-devel")
CU126 = _template("t-126", "Vidaio", "nvidia/cuda", "12.6.0-cudnn-devel-ubuntu24.04")
CU121 = _template("t-121", "Old", "pytorch/pytorch", "2.1.0-cuda12.1-cudnn8-runtime")
CU117 = _template("t-117", "Older", "pytorch/pytorch", "1.13.0-cuda11.7-cudnn8-runtime")
LATEST = _template("t-lat", "vLLM", "elaich/sglang", "latest")
TEMPLATES = [CU130, CU128, CU126, CU121, CU117, LATEST]



@pytest.fixture
def fake_lium(monkeypatch):
    class _Lium:
        def __init__(self, *a, **k):
            pass

        def templates(self, search=None):
            if not search:
                return list(TEMPLATES)
            return [t for t in TEMPLATES if search.lower() in t.name.lower() or search.lower() in t.docker_image.lower()]

    monkeypatch.setattr(templates_module, "Lium", _Lium)
    return _Lium


# --- derivation --------------------------------------------------------------------------

@pytest.mark.parametrize("image, tag, expected", [
    ("daturaai/pytorch", "2.12.0-py3.12-cuda13.0.2-devel-ubuntu24.04-dind", 13.0),
    ("pytorch/pytorch", "2.7.1-cuda12.8-cudnn9-devel", 12.8),
    ("primarchleman/lium-pytorch", "2.12.0-cu130-dind-ubuntu24.04-docker27", 13.0),
    ("nvidia/cuda", "12.6.0-cudnn-devel-ubuntu24.04", None),      # no "cu" prefix: not claimed
    ("nvidia/cuda", "13.0.2-devel-ubuntu22.04", None),
    ("modelscope", "ubuntu22.04-cuda13.0.3-py312-torch2.11.0", 13.0),
    ("elaich/sglang", "latest", None),
    ("something", "cuda_12_6", None),                             # ambiguous separators are not guessed
    ("vendor/cuda12.8-runtime", "latest", 12.8),                  # tag says nothing: the image name is read
    ("vendor/cuda12.8-runtime", "cu130", 13.0),                   # the tag wins over the image name
])
def test_cuda_version_from_tag(image, tag, expected):
    assert arch.cuda_version(image, tag) == expected


@pytest.mark.parametrize("image, tag, expected", [
    ("daturaai/pytorch", "2.12.0-py3.12-cuda13.0.2-devel", "2.12.0"),
    ("pytorch/pytorch", "2.7.1-cuda12.8-cudnn9-devel", "2.7.1"),
    ("modelscope", "ubuntu22.04-cuda13.0.3-py312-torch2.11.0-vllm0.23.0", "2.11.0"),
    ("nvidia/cuda", "12.6.0-cudnn-devel-ubuntu24.04", None),
    ("pytorch/pytorch", "latest", None),
])
def test_torch_version_from_tag(image, tag, expected):
    assert arch.torch_version(image, tag) == expected


@pytest.mark.parametrize("cuda, support", [
    (13.0, "hopper+blackwell"), (12.8, "hopper+blackwell"), (12.6, "hopper"), (11.8, "hopper"),
    (11.7, "pre-hopper"), (None, None),
])
def test_arch_support_thresholds(cuda, support):
    assert arch.arch_support(cuda) == support


def test_supports_blackwell_needs_cu128_hopper_accepts_both():
    assert arch.supports("blackwell", "hopper+blackwell") and not arch.supports("blackwell", "hopper")
    assert arch.supports("hopper", "hopper+blackwell") and arch.supports("hopper", "hopper")
    assert not arch.supports("hopper", None) and not arch.supports("hopper", "pre-hopper")
    with pytest.raises(ValueError):
        arch.supports("volta", "hopper")


# --- list ----------------------------------------------------------------------------------

def _names(result):
    assert result.exit_code == 0, result.output
    return [t["name"] for t in json.loads(result.output)]


def test_templates_json_carries_cuda_torch_and_arch(fake_lium):
    result = CliRunner().invoke(cli, ["templates", "--format", "json"])

    rows = {t["name"]: t for t in json.loads(result.output)}
    assert rows["Pytorch DinD"]["cuda_version"] == 13.0
    assert rows["Pytorch DinD"]["torch_version"] == "2.12.0"
    assert rows["Pytorch DinD"]["arch"] == "hopper+blackwell"
    assert rows["Old"]["arch"] == "hopper" and rows["vLLM"]["arch"] is None


def test_templates_arch_blackwell_keeps_only_cu128_plus(fake_lium):
    assert _names(CliRunner().invoke(cli, ["templates", "--arch", "blackwell", "--json"])) == ["Pytorch DinD", "PyTorch 2.7"]


def test_templates_arch_hopper_excludes_unknown_and_pre_hopper(fake_lium):
    assert _names(CliRunner().invoke(cli, ["templates", "--arch", "hopper", "--json"])) == ["Pytorch DinD", "PyTorch 2.7", "Old"]


def test_templates_search_still_works_as_a_bare_argument(fake_lium):
    assert _names(CliRunner().invoke(cli, ["templates", "vidaio", "--json"])) == ["Vidaio"]
    assert _names(CliRunner().invoke(cli, ["templates", "pytorch", "--arch", "blackwell", "--json"])) == ["Pytorch DinD", "PyTorch 2.7"]


def test_templates_table_has_the_runs_on_column_and_a_legend(fake_lium):
    result = CliRunner().invoke(cli, ["templates"])

    assert result.exit_code == 0, result.output
    assert "Runs on" in result.output and "Blackwell" in result.output
    assert "CUDA 12.8+" in result.output
    # the cells, not only the header and legend (CliRunner renders at 80 columns, so the cell may fold at a space)
    assert "13.0" in result.output and "Hopper+Bl" in result.output and "pre-Hoppe" in result.output
    assert arch.runs_on_cell(13.0, arch.HOPPER_AND_BLACKWELL) == "13.0 Hopper+Blackwell"
    assert arch.runs_on_cell(12.1, arch.HOPPER_ONLY) == "12.1 Hopper only"
    assert arch.runs_on_cell(None, None) == "?"


def test_templates_table_fits_80_columns():
    """Two more columns must not bring back the 11-line-per-template fold #217 removed."""
    from rich.console import Console

    from lium.cli.templates.display import build_templates_table

    table, _ = build_templates_table(TEMPLATES)
    console = Console(width=80, record=True, force_terminal=False)
    console.print(table)
    lines = [line for line in console.export_text().splitlines() if line.strip()]
    # #217's five-column table renders these six in 19 lines; one more column may cost a line each, not five
    assert len(lines) <= 19 + len(TEMPLATES), "\n".join(lines)


def test_templates_arch_with_no_match_explains_how_it_is_derived(fake_lium, monkeypatch):
    monkeypatch.setattr(fake_lium, "templates", lambda self, search=None: [LATEST])

    result = CliRunner().invoke(cli, ["templates", "--arch", "blackwell"])

    assert result.exit_code == 0
    assert "No templates whose CUDA build is known to run on blackwell" in result.output
    assert "image tag" in result.output


def test_templates_help_lists_arch(fake_lium):
    result = CliRunner().invoke(cli, ["templates", "--help"])

    assert "--arch" in result.output
