"""Clusters command group: list rentable RDMA fabrics, rent N nodes as one cluster, inspect and remove."""

import click

from .command import (
    FORMAT_OPTION,
    clusters_list_command,
    clusters_ps_command,
    clusters_rm_command,
    clusters_show_command,
    clusters_up_command,
)


@click.group(invoke_without_command=True)
@FORMAT_OPTION
@click.pass_context
def clusters_command(ctx, output_format: str, json_output: bool):
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
        ctx.invoke(clusters_list_command, output_format=output_format, json_output=json_output)


clusters_command.add_command(clusters_list_command)
clusters_command.add_command(clusters_up_command)
clusters_command.add_command(clusters_ps_command)
clusters_command.add_command(clusters_show_command)
clusters_command.add_command(clusters_rm_command)

__all__ = ["clusters_command"]
