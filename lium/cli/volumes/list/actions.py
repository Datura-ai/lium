from lium.cli.actions import ActionResult


class GetVolumesAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium = ctx["lium"]

        volumes = lium.volumes()
        return ActionResult(
            ok=True,
            data={"volumes": volumes}
        )
