"""``lium provider …`` CLI subgroup.

Adds the provider-side namespace alongside the renter ``lium mine`` command.
ADR-002 (plan: ``mining-agent-cli-plan.md``) explains why a sibling Click
group was preferred over a separate distribution.
"""

from lium.cli.provider.command import provider_command

__all__ = ["provider_command"]
