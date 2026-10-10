"""`Lium.wait_template_ready`: a template the platform will rent is ready.

New templates are no longer verified: a public one stays CREATED (UPDATED after an edit) and rents
right away, so waiting for VERIFY_SUCCESS timed out on every public template.
"""

import pytest

from lium.sdk import LiumError, Template
from lium.sdk import client as client_module
from lium.sdk.client import Lium
from lium.sdk.utils import generate_huid


def _lium(monkeypatch, status):
    template = Template(id="t-1", huid=generate_huid("t-1"), name="mine", docker_image="me/img",
                        docker_image_tag="latest", category="CUSTOM", status=status)
    lium = Lium.__new__(Lium)  # no config, no network: templates() is the only call
    monkeypatch.setattr(lium, "templates", lambda only_my=False: [template], raising=False)
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    return lium, template


@pytest.mark.parametrize("status", ["CREATED", "UPDATED", "VERIFY_SUCCESS"])
def test_a_usable_template_is_returned_at_once(monkeypatch, status):
    lium, template = _lium(monkeypatch, status)

    assert lium.wait_template_ready("t-1", timeout=5) is template


def test_a_pending_verification_is_still_waited_for(monkeypatch):
    lium, _template = _lium(monkeypatch, "VERIFY_PENDING")
    clock = iter(range(0, 100, 10))
    monkeypatch.setattr(client_module.time, "time", lambda: next(clock))

    assert lium.wait_template_ready("t-1", timeout=30) is None


def test_a_failed_verification_raises(monkeypatch):
    lium, _template = _lium(monkeypatch, "VERIFY_FAILED")

    with pytest.raises(LiumError, match="verification failed"):
        lium.wait_template_ready("t-1", timeout=5)
