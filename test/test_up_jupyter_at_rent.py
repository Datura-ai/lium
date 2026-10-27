"""DAH-3698: `lium up --jupyter` asks for Jupyter at rent, so the server holds its port and installs it; the CLI then only
waits. On a tight split node the pod has the SSH port alone, and the old after-rent install found no port for Jupyter."""

from types import SimpleNamespace

from lium.cli.up.actions import InstallJupyterAction, RentPodAction


def _pod(status):
    return SimpleNamespace(id="p1", huid="h", name="n", ports={"22": 40000}, jupyter_installation_status=status, jupyter_url=None)


def test_a_pod_rented_with_jupyter_is_only_waited_for():
    done = SimpleNamespace(id="p1", huid="h", name="n", jupyter_installation_status="SUCCESS", jupyter_url="http://x/?token=t")
    installs = []
    lium = SimpleNamespace(install_jupyter=lambda *a, **k: installs.append(a), ps=lambda: [done])

    result = InstallJupyterAction().execute({"lium": lium, "pod": _pod("INSTALLING")})

    assert result.ok and result.data["jupyter_url"] == "http://x/?token=t"
    assert installs == []


def test_a_pod_without_it_still_needs_a_spare_port():
    lium = SimpleNamespace(install_jupyter=lambda *a, **k: None, ps=lambda: [])

    result = InstallJupyterAction().execute({"lium": lium, "pod": _pod(None)})

    assert not result.ok and "only SSH" in result.error


def test_up_with_jupyter_asks_for_it_at_rent():
    sent = {}
    lium = SimpleNamespace(up=lambda **kw: sent.update(kw) or {"id": "p1"})
    node = SimpleNamespace(id="e1", huid="h", price_per_hour=1.0)

    RentPodAction().execute({"lium": lium, "executor": node, "jupyter": True})
    assert sent["enable_jupyter"] is True
    RentPodAction().execute({"lium": lium, "executor": node})
    assert sent["enable_jupyter"] is False
