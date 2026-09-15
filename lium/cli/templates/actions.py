from lium.cli.actions import ActionResult


class GetTemplatesAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium = ctx["lium"]
        search = ctx.get("search")

        templates = lium.templates(search)
        return ActionResult(
            ok=True,
            data={"templates": templates}
        )
