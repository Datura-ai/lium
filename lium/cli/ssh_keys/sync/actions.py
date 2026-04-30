"""Actions for `lium ssh-keys sync`."""

from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, SSHKey
from lium.sdk.exceptions import LiumError
from lium.sdk.ssh_key_cache import fingerprint, load_cache, save_cache


class SyncSSHKeysAction:
    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        try:
            local_pubkeys = [pk.strip() for pk in lium.config.ssh_public_keys if pk.strip()]
            server_keys = lium.list_ssh_keys()
            server_index = {k.public_key.strip(): k for k in server_keys if k.public_key}
            default_name = lium.default_ssh_key_name()

            already_registered = 0
            registered: List[SSHKey] = []
            for pk in local_pubkeys:
                if pk in server_index:
                    already_registered += 1
                    continue
                try:
                    new_key = lium.register_ssh_key(name=default_name, public_key=pk)
                    registered.append(new_key)
                    server_index[pk] = new_key
                except LiumError as exc:
                    return ActionResult(
                        ok=False,
                        data={},
                        error=f"Failed to register a key: {exc}",
                    )

            cached = load_cache(lium.config)
            new_fps = set(cached) | {fingerprint(pk) for pk in local_pubkeys}
            if new_fps != cached:
                try:
                    save_cache(lium.config, new_fps)
                except OSError:
                    pass

            legacy = [k for k in server_index.values() if (k.name or "").startswith("sdk-")]

            return ActionResult(
                ok=True,
                data={
                    "registered": registered,
                    "legacy": legacy,
                    "already_registered": already_registered,
                    "default_name": default_name,
                },
            )
        except Exception as exc:
            return ActionResult(ok=False, data={}, error=str(exc))
