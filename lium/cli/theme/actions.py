from lium.cli.actions import ActionResult


class GetThemeAction:

    def execute(self, ctx: dict) -> ActionResult:
        console = ctx["console"]

        current = console.get_current_theme_name()
        resolved = console.get_resolved_theme_name()

        return ActionResult(
            ok=True,
            data={
                "current": current,
                "resolved": resolved
            }
        )


class SwitchThemeAction:

    def execute(self, ctx: dict) -> ActionResult:
        console = ctx["console"]
        theme_name = ctx["theme_name"]

        old_theme = console.get_current_theme_name()
        console.switch_theme(theme_name)

        resolved = None
        if theme_name == "auto":
            resolved = console.get_resolved_theme_name()

        return ActionResult(
            ok=True,
            data={
                "old_theme": old_theme,
                "new_theme": theme_name,
                "resolved": resolved
            }
        )
