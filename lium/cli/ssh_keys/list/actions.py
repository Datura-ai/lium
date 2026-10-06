"""Actions for `lium ssh-keys list`."""

from lium.cli.actions import ActionResult


class GetSSHKeysAction:
    def execute(self, ctx: dict) -> ActionResult:
        lium = ctx["lium"]
        keys = lium.list_ssh_keys()
        return ActionResult(ok=True, data={"keys": keys})
