import configparser
import re
from typing import Optional

from lium.cli.actions import ActionResult
from lium.cli.config.get.command import mask_value
from lium.cli.settings import config


def mask_config_text(content: str) -> Optional[str]:
    """The config file's text with every ``api_key`` value and ``[session] token`` masked.

    The file is read the way the CLI reads it (``ConfigParser(interpolation=None)``, as in
    ``lium/cli/settings.py``: lower-cased options, indented continuation lines joined into the
    value), so the set of secret values is exactly what ``config get`` would mask; then each of
    those values, wherever it stands alone in the text (after a delimiter or whitespace, before
    whitespace or the end; a comment too), is replaced by ``mask_value``'s shortened form, and a
    continuation line of a secret by ``...``. Comments, blank lines and the order stay as they are.
    ``None`` when the file does not parse for that parser (a duplicate option or section too):
    the command prints the path and a note then, rather than a file that may hold a key.
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return None
    secrets = []
    sections = [configparser.DEFAULTSECT, *parser.sections()] if parser.defaults() else parser.sections()
    for section in sections:
        for option, value in parser.items(section):
            masked = mask_value(value, f"{section}.{option}")
            if value and masked != value:
                secrets.append((value, masked))
    out = content
    for value, masked in sorted(secrets, key=lambda pair: -len(pair[0])):
        pieces = [piece.strip() for piece in value.split("\n") if piece.strip()]
        for index, piece in enumerate(pieces):
            # only where it stands alone (after a delimiter or whitespace, before whitespace or the
            # end), so a short value does not eat part of another value or path
            out = re.sub(
                r"(?<![^\s=:])" + re.escape(piece) + r"(?![^\s])",
                lambda _m, text=(masked if index == 0 else "..."): text,
                out,
            )
    return out


class ShowConfigAction:
    """Show all config."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute config show."""
        config_path = config.get_config_path()

        if not config_path.exists():
            return ActionResult(ok=True, data={"config_path": config_path, "content": ""})

        content = mask_config_text(config_path.read_text())
        if content is None:
            return ActionResult(
                ok=True,
                data={
                    "config_path": config_path,
                    "content": "",
                    "note": "not shown: the file does not parse as a config file; open it to read it",
                },
            )

        return ActionResult(
            ok=True,
            data={
                "config_path": config_path,
                "content": content
            }
        )
