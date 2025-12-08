"""Main CLI entry point for Lium."""
import click
import os
from importlib.metadata import version, PackageNotFoundError
from .themed_console import ThemedConsole
from .init.command import init_command
from .ls import ls_command
from .templates import templates_command
from .up import up_command
from .ps import ps_command
from .commands.exec import exec_command
from .ssh.command import ssh_command
from .rm import rm_command
from .logs import logs_command
from .reboot import reboot_command
from .scp.command import scp_command
from .rsync import rsync_command
from .theme import theme_command
# from .commands.compose import compose_command  # Disabled for beta.1
from .config import config_command
# from .commands.image import image_command  # Disabled for beta.1
from .fund import fund_command
from .bk import bk_command
from .commands.mine import mine_command
from .volumes import volumes_command
from .schedules import schedules_command
from .update.command import update_command
from .port_forward import port_forward_command
from .plugins import load_plugins


def get_version():
    """Get version from package metadata."""
    try:
        return version("lium.io")
    except PackageNotFoundError:
        return "unknown"


@click.group(invoke_without_command=True)
@click.version_option(version=get_version(), prog_name="lium")
@click.pass_context
def cli(ctx):
    """Lium CLI - Unix-style GPU pod management.
    
    A clean, Unix-style command-line interface for managing GPU pods.
    Run individual commands or use 'lium --help' to see all available commands.
    """
    # Make ThemedConsole available to all commands via context
    ctx.ensure_object(dict)
    ctx.obj['console'] = ThemedConsole()
    
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# Register core commands
cli.add_command(init_command)
cli.add_command(ls_command)
cli.add_command(templates_command)
cli.add_command(up_command)
cli.add_command(ps_command)
cli.add_command(exec_command)
cli.add_command(ssh_command)
cli.add_command(rm_command)
cli.add_command(logs_command)
cli.add_command(reboot_command)
cli.add_command(scp_command)
cli.add_command(rsync_command)
cli.add_command(theme_command)
cli.add_command(config_command)
# cli.add_command(image_command)  # Disabled for beta.1
cli.add_command(fund_command)
cli.add_command(bk_command, name="bk")
cli.add_command(mine_command)
cli.add_command(volumes_command)
cli.add_command(schedules_command, name="schedules")
cli.add_command(update_command)
cli.add_command(port_forward_command)

# Add compose placeholder (will be overridden if plugin is installed)
# cli.add_command(compose_command)  # Disabled for beta.1

# Load any installed plugins
# Plugins can override existing commands or add new ones
load_plugins(cli)


def main():
    """Main entry point for the CLI."""
    if not os.environ.get('_LIUM_COMPLETE'):
        from .completion import ensure_completion
        ensure_completion()
    
    cli()


if __name__ == "__main__":
    main()
