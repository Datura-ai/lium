"""Fund account command (retired).

NowPayments crypto invoices and the legacy on-chain TAO wallet transfer were the
two CLI funding flows. Both have been retired in favour of the dedicated
TaoMarketCap payment service (DAH-2154); account funding now happens from the
Lium dashboard. This command is kept as a deprecation notice so existing
scripts get a clear message instead of an unknown-command error.
"""

import click

from lium.cli import ui

FUNDING_URL = "https://lium.io/billing"

DEPRECATION_MESSAGE = (
    "CLI account funding has been retired. The NowPayments crypto invoice flow "
    "and the legacy TAO wallet transfer are no longer available from the CLI."
)


@click.command(
    "fund",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def fund_command(ctx: click.Context, args: tuple[str, ...]):
    """Fund your Lium account.

    This command has been retired. Top up your balance from the Lium dashboard
    instead, and use `lium balance` to check your current balance.
    """
    ui.error(DEPRECATION_MESSAGE)
    ui.dim(f"Fund your account at {FUNDING_URL}")
    ui.dim("Check your balance with: lium balance")
    ctx.exit(1)
