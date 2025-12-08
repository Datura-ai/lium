from lium.cli.actions import ActionResult
from lium.cli.settings import config


class SetConfigAction:
    """Set config value."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute config set."""
        key: str = ctx["key"]
        value: str = ctx["value"]

        config.set(key, value)

        new_value = config.get(key)

        if new_value is None:
            return ActionResult(ok=False, data={}, error=f"Failed to set {key}")

        return ActionResult(ok=True, data={"value": new_value})
