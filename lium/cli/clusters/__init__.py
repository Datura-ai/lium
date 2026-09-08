"""Clusters command group: list rentable RDMA fabrics, rent N nodes as one cluster, inspect and remove."""

import click

from .command import (
    clusters_list_command,
    clusters_ps_command,
    clusters_rm_command,
    clusters_show_command,
    clusters_up_command,
)


@click.group(invoke_without_command=True)
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]), default="table", show_default=True,
    help="Output format of the default listing. 'json' emits machine-readable JSON to stdout.",
)
@click.pass_context
def clusters_command(ctx, output_format: str):
    """Multi-node clusters on one InfiniBand/RoCE fabric.

    \b
    Examples:
      lium clusters                         # fabrics with free nodes (same as 'clusters list')
      lium clusters --format json           # for scripts and agents
      lium clusters up 1 --nodes 2 -n job   # rent 2 nodes of fabric #1 as one cluster
      lium clusters ps                      # your clusters
      lium clusters show job --hostfile     # members, overlay IPs, mpirun hostfile
      lium clusters rm job -y               # remove every member
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(clusters_list_command, output_format=output_format)


clusters_command.add_command(clusters_list_command)
clusters_command.add_command(clusters_up_command)
clusters_command.add_command(clusters_ps_command)
clusters_command.add_command(clusters_show_command)
clusters_command.add_command(clusters_rm_command)

__all__ = ["clusters_command"]
