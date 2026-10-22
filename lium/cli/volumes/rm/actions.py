from lium.cli.actions import ActionResult


class RemoveVolumesAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium = ctx["lium"]
        volumes_to_remove = ctx["volumes_to_remove"]

        failures = []

        for idx, volume_data in volumes_to_remove:
            volume_id = volume_data['id']
            volume_huid = volume_data['huid']

            try:
                lium.volume_delete(volume_id)
            except Exception as e:
                failures.append(f"{volume_huid} ({e})")

        return ActionResult(
            ok=len(failures) == 0,
            data={"failures": failures}
        )
