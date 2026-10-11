"""`lium job run`: the CLI face of `Lium.run_job`."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.job import command as job_module
from lium.sdk.bounded_job import JobResult


class _FakeLium:
    calls = []
    result = JobResult(status="succeeded", name="job-1", exit_code=0, stdout="done\n", cleanup="removed",
                       cost_usd=0.04, seconds=42.0, outputs={"/root/model.pt": "model.pt"})

    def run_job(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def fake(monkeypatch):
    _FakeLium.calls = []
    monkeypatch.setattr(job_module, "Lium", _FakeLium)
    return _FakeLium


def _run(*args):
    return CliRunner().invoke(cli, ["job", "run", *args])


def test_job_run_passes_the_job_to_run_job_and_exits_0(fake):
    out = _run("python train.py", "--gpu", "H100", "--count", "2", "--budget", "5", "--deadline", "30m",
               "--input", "train.py:/root/train.py", "--output", "/root/model.pt:model.pt", "--env", "SEED=1",
               "--max-price-per-gpu-hour", "3", "--format", "json")

    assert out.exit_code == 0, out.output
    (kwargs,) = fake.calls
    assert kwargs == dict(
        command="python train.py", gpu_type="H100", gpu_count=2, max_cost_usd=5.0, deadline_s=1800.0,
        inputs={"train.py": "/root/train.py"}, outputs={"/root/model.pt": "model.pt"}, env={"SEED": "1"},
        max_price_per_gpu_hour=3.0,
    )
    assert json.loads(out.output)["status"] == "succeeded"


@pytest.mark.parametrize(
    "status, exit_code, expected",
    [("failed", 3, 3), ("timed_out", 124, 124), ("budget_exhausted", None, 124), ("error", None, 1),
     ("rent_failed", None, 1)],
)
def test_job_run_exit_code_follows_the_job(fake, monkeypatch, status, exit_code, expected):
    monkeypatch.setattr(fake, "result", JobResult(status=status, name="job-1", exit_code=exit_code))

    out = _run("false", "--gpu", "H100", "--budget", "1", "--deadline", "60", "--format", "json")

    assert out.exit_code == expected


@pytest.mark.parametrize("args", [["--deadline", "soon"], ["--deadline", "0"], ["--output", "model.pt"]])
def test_job_run_refuses_bad_arguments_before_renting(fake, args):
    out = _run("true", "--gpu", "H100", "--budget", "1", *(["--deadline", "60"] if args[0] != "--deadline" else []), *args)

    assert out.exit_code == 2 and fake.calls == []
