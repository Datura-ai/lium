"""Signup command implementation."""

import json

import click

from lium.cli import ui
from lium.cli.init.actions import SetupSshKeyAction
from lium.cli.utils import CliFailure, handle_errors
from .actions import MintBillingKeyAction, SignupAction, generate_password


def _credit_line(credit_granted: bool | None) -> str:
    # wording follows the backend's signup_credit_granted flag; older backends omit it, so nothing is asserted then
    if credit_granted is True:
        return "A $5 signup credit was granted — check it with 'lium balance'."
    if credit_granted is False:
        return ("No signup credit was granted — it is granted once per IP address and can be disabled. "
                "Fund the account before renting.")
    return "Check the balance with 'lium balance' and fund the account before renting."


@click.command("signup")
@click.option("--email", required=True, help="The user's real email — the verification link is sent there.")
@click.option("--name", "display_name", default=None, help="Display name (defaults to the email's local part).")
@click.option("--password", default=None, envvar="LIUM_SIGNUP_PASSWORD",
              help="Account password (generated when omitted). Falls back to LIUM_SIGNUP_PASSWORD.")
@click.option("--billing-key", "billing_key", is_flag=True,
              help="Also mint a key holding only the 'billing' scope (top-ups, no pods), kept as [api] billing_api_key.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def signup_command(email: str, display_name: str | None, password: str | None, billing_key: bool, json_output: bool):
    """Create a Lium account and store the API key it mints.

    Non-interactive: safe to run from an agent. The API key is written to
    ~/.lium/config.ini, so `lium ls` and `lium up` work right after.

    To set your own password, prefer LIUM_SIGNUP_PASSWORD over --password:
    a flag value is left behind in the shell history and in `ps` output.

    --billing-key also mints a second key that holds only the `billing` scope, so an
    agent can top up with `lium topup link` / `lium topup create` without a browser
    login; the account's first key (read, rent, manage) never moves money. A billing
    key that cannot be minted is reported, not fatal: the account and its first key stand.

    \b
    Examples:
      lium signup --email ada@example.com
      lium signup --email ada@example.com --json
      lium signup --email ada@example.com --billing-key --json
      LIUM_SIGNUP_PASSWORD=... lium signup --email ada@example.com
    """
    password = password or generate_password()
    display_name = display_name or email.split("@")[0]

    signup_result = SignupAction(email=email, password=password, display_name=display_name).execute({})
    if not signup_result.ok:
        # the account may already exist server-side; losing the generated password would make it unreachable
        if signup_result.data.get("account_may_exist"):
            raise CliFailure(
                "signup_failed",
                f"{signup_result.error} Log in at https://lium.io with "
                f"{email} / {password} and copy your API key from the dashboard.",
                data={"email": email, "password": password},
            )
        raise CliFailure("signup_failed", signup_result.error)

    ssh_result = SetupSshKeyAction().execute({})
    credit_granted = signup_result.data.get("signup_credit_granted")
    billing_result = MintBillingKeyAction(email=email, password=password).execute({}) if billing_key else None

    if json_output:
        billing_fields = {}
        if billing_result is not None:
            billing_fields = {
                "billing_api_key": billing_result.data.get("billing_api_key"),
                "billing_key_configured": billing_result.ok,
            }
            if not billing_result.ok:
                billing_fields["billing_key_error"] = billing_result.error
        click.echo(json.dumps({
            "email": email,
            "password": password,
            "api_key": signup_result.data["api_key"],
            "ssh_key_configured": ssh_result.ok,
            "signup_credit_granted": credit_granted,
            **billing_fields,
            "next_steps": [
                _credit_line(credit_granted),
                "Top up: lium topup link -a 10 --wait 900 (a person pays by card) or "
                "lium topup create -a 10 -c USDC -n base --wait 900 (send the stablecoin).",
                "Then: lium ls, lium up <node-id>.",
                "The verification link in the confirmation email does not gate renting — it confirms "
                "the address so password resets and account emails reach the user.",
            ],
        }, sort_keys=True))
        return

    ui.success(f"Account created for {email}")
    ui.print(f"\n  password: {password}")
    ui.dim("  Save it — it is the dashboard login at https://lium.io\n")
    ui.info("API key stored in ~/.lium/config.ini")
    if not ssh_result.ok:
        ui.warning(f"SSH key not configured: {ssh_result.error}")
    if billing_result is not None:
        if billing_result.ok:
            ui.info("Billing key (top-ups only) stored in ~/.lium/config.ini as billing_api_key")
        else:
            ui.warning(f"Billing key not minted: {billing_result.error}")

    ui.print("")
    ui.info("Before the first rental:")
    ui.print(f"  1. {_credit_line(credit_granted)}")
    ui.print("  2. Then 'lium ls' and 'lium up <node-id>'.")
    ui.print("")
    ui.dim("  The verification link in the confirmation email does not gate renting — it confirms")
    ui.dim("  the address so password resets and account emails reach you.")
