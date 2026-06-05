import time
from typing import Any

from lium.cli.actions import ActionResult
from .validation import subtensor_class, wallet_class

# Lium's on-chain funding coldkey for the legacy TAO transfer. The alpha path no
# longer uses this constant — it resolves the destination coldkey from the pay API
# at fund time (see ``_alpha_fund``) so a rotated address can never silently send
# alpha into a black hole.
LIUM_FUNDING_ADDRESS = "5FqACMtcegZxxopgu1g7TgyrnyD8skurr9QDPLPhxNQzsThe"


def _select_free_alpha(stake_infos, hotkey_ss58, netuid):
    """Select the single StakeInfo for ``hotkey_ss58`` on ``netuid`` and return free alpha.

    Free alpha is ``stake - locked`` (both alpha-denominated Balances). The match
    must be on hotkey AND netuid; a coldkey can hold alpha under several hotkeys
    and across several subnets, so a loose match would gate on the wrong balance.
    ``netuid`` is the API-resolved subnet — never a hardcoded constant.

    Returns:
        ``(free_balance, "")`` on success, or ``(None, error_message)`` when there
        is no matching stake (nothing to transfer) or — unexpectedly — more than
        one matching row (ambiguous; never silently pick one).
    """
    rows = [
        s
        for s in (stake_infos or [])
        if s.hotkey_ss58 == hotkey_ss58 and s.netuid == netuid
    ]
    if not rows:
        return None, (
            f"No free alpha: no stake found for hotkey {hotkey_ss58} on netuid {netuid}"
        )
    if len(rows) > 1:
        return None, (
            f"Unexpected multiple stake entries for hotkey {hotkey_ss58} "
            f"on netuid {netuid}; aborting"
        )
    return rows[0].stake - rows[0].locked, ""


def _movement_fee(subtensor, amount_bal, netuid):
    """Best-effort on-chain stake-movement fee for the alpha transfer.

    The transfer stays on one subnet (origin == destination netuid). This is purely
    advisory: when available the fee is folded into the free-alpha gate and shown to
    the user; when the node can't answer, the caller proceeds on ``free >= amount``
    rather than blocking.

    Returns:
        ``(fee_balance, True)`` when obtained, else ``(None, False)``.
    """
    try:
        fee = subtensor.get_stake_movement_fee(
            origin_netuid=netuid,
            destination_netuid=netuid,
            amount=amount_bal,
        )
        return fee, True
    except Exception:
        return None, False


class LoadWalletAction:
    """Load and prepare Bittensor wallet."""

    def execute(self, ctx: dict) -> ActionResult:
        """Load wallet.

        Context:
            bt: bittensor module
            wallet_name: str
        """
        import bittensor as bt

        try:
            wallet_name = ctx["wallet_name"]
            # bittensor >=8 renamed the factory to ``bt.Wallet``; resolve either.
            bt_wallet = wallet_class(bt)(wallet_name)
            wallet_address = bt_wallet.coldkeypub.ss58_address

            return ActionResult(
                ok=True,
                data={
                    "wallet": bt_wallet,
                    "address": wallet_address
                }
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class CheckWalletRegistrationAction:
    """Check if wallet is registered with Lium."""

    def execute(self, ctx: dict) -> ActionResult:
        """Check wallet registration.

        Context:
            lium: Lium SDK instance
            wallet_address: str
            bt_wallet: bittensor wallet
        """
        lium = ctx["lium"]
        wallet_address = ctx["wallet_address"]
        bt_wallet = ctx["bt_wallet"]

        try:
            user_wallets = lium.wallets()
            wallet_addresses = [w.get('wallet_hash', '') for w in user_wallets]

            needs_registration = wallet_address not in wallet_addresses

            app_id = None
            if needs_registration:
                # add_wallet returns (app_id, customer_id) parsed from the same
                # /tao/create-transfer round-trip; the alpha flow reuses app_id so
                # it never issues a second create-transfer. The TAO flow ignores it.
                registration = lium.add_wallet(bt_wallet)
                if registration:
                    app_id = registration[0]
                time.sleep(2)  # Allow registration to complete

            return ActionResult(
                ok=True,
                data={"registered": not needs_registration, "app_id": app_id}
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class ExecuteTransferAction:
    """Execute TAO transfer to Lium funding address."""

    LIUM_FUNDING_ADDRESS = LIUM_FUNDING_ADDRESS

    def execute(self, ctx: dict) -> ActionResult:
        """Execute transfer.

        Context:
            bt: bittensor module
            bt_wallet: bittensor wallet
            tao_amount: float
        """
        import bittensor as bt

        bt_wallet = ctx["bt_wallet"]
        tao_amount = ctx["tao_amount"]

        try:
            subtensor = subtensor_class(bt)()

            resp = subtensor.transfer(
                wallet=bt_wallet,
                destination_ss58=self.LIUM_FUNDING_ADDRESS,
                amount=bt.Balance.from_tao(tao_amount),
            )

            # bittensor >=8 returns an ExtrinsicResponse (.success/.message); older
            # versions returned a bare bool. Accept either.
            ok = getattr(resp, "success", resp)
            if not ok:
                msg = (
                    getattr(resp, "message", None)
                    or getattr(resp, "error", None)
                    or "Transfer failed"
                )
                return ActionResult(ok=False, data={}, error=str(msg))

            return ActionResult(ok=True, data={})
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class WaitForBalanceUpdateAction:
    """Wait for balance update after transfer."""

    TIMEOUT = 300  # 5 minutes

    def execute(self, ctx: dict) -> ActionResult:
        """Wait for balance to update.

        Context:
            lium: Lium SDK instance
            current_balance: float
        """
        lium = ctx["lium"]
        current_balance = ctx["current_balance"]

        start_time = time.time()

        while time.time() - start_time < self.TIMEOUT:
            try:
                new_balance = lium.balance()
                if new_balance > current_balance:
                    funded_amount = new_balance - current_balance
                    return ActionResult(
                        ok=True,
                        data={
                            "new_balance": new_balance,
                            "funded_amount": funded_amount
                        }
                    )
            except Exception:
                pass  # Ignore temporary API errors

            time.sleep(5)

        return ActionResult(
            ok=False,
            data={},
            error=f"Balance not updated after {self.TIMEOUT}s timeout"
        )


class CheckFreeAlphaAction:
    """Phase A guardrail: read free SN51 alpha for display before the confirm prompt.

    This is NOT the authoritative gate — balances can change between display and
    signing, so the binding check is re-done inside :class:`ExecuteAlphaTransferAction`
    immediately before the transfer.

    Context:
        bt: bittensor module
        coldkey_ss58: str
        hotkey_ss58: str
        amount_bal: Balance (alpha, unit = resolved netuid)
        netuid: int (API-resolved subnet)
        dest_coldkey: str (API-resolved Lium funding coldkey)
    """

    def execute(self, ctx: dict) -> ActionResult:
        bt = ctx["bt"]
        coldkey = ctx["coldkey_ss58"]
        hotkey = ctx["hotkey_ss58"]
        amount = ctx["amount_bal"]
        netuid = ctx["netuid"]
        dest_coldkey = ctx["dest_coldkey"]

        try:
            subtensor = subtensor_class(bt)()
            stake_infos = subtensor.get_stake_info_for_coldkey(coldkey)
            free, err = _select_free_alpha(stake_infos, hotkey, netuid)
            if err:
                return ActionResult(ok=False, data={}, error=err)

            fee, modeled = _movement_fee(subtensor, amount, netuid)
            return ActionResult(
                ok=True,
                data={"free": free, "fee": fee, "fee_modeled": modeled},
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class ExecuteAlphaTransferAction:
    """Authoritative gate + execute: transfer free SN51 alpha to the Lium coldkey.

    Constructs a single subtensor, performs a FRESH read of free alpha immediately
    before signing (closing the time-of-check/time-of-use window), gates on
    ``free_alpha >= requested_alpha``, then ``transfer_stake``s on the same instance.
    The movement fee is denominated in TAO and paid from the coldkey separately from
    the alpha being moved, so it is purely advisory (display/JSON) and never folded
    into the alpha gate — the chain rejects (surfaced via ExtrinsicResponse) if the
    coldkey lacks the TAO to cover it.

    Context:
        bt: bittensor module
        bt_wallet: bittensor wallet (signs the extrinsic)
        coldkey_ss58: str
        hotkey_ss58: str
        amount_bal: Balance (alpha, unit = resolved netuid)
        netuid: int (API-resolved subnet; drives the transfer)
        dest_coldkey: str (API-resolved Lium funding coldkey; the transfer destination)
    """

    def execute(self, ctx: dict) -> ActionResult:
        bt = ctx["bt"]
        bt_wallet = ctx["bt_wallet"]
        coldkey = ctx["coldkey_ss58"]
        hotkey = ctx["hotkey_ss58"]
        amount = ctx["amount_bal"]
        netuid = ctx["netuid"]
        dest_coldkey = ctx["dest_coldkey"]

        try:
            subtensor = subtensor_class(bt)()
            stake_infos = subtensor.get_stake_info_for_coldkey(coldkey)
            free, err = _select_free_alpha(stake_infos, hotkey, netuid)
            if err:
                return ActionResult(ok=False, data={}, error=err)

            # The movement fee is TAO-denominated and paid from the coldkey, NOT
            # deducted from the alpha being moved — so the gate is a pure alpha-vs-alpha
            # comparison. The fee is fetched only for display/JSON.
            fee, modeled = _movement_fee(subtensor, amount, netuid)

            if free < amount:
                return ActionResult(
                    ok=False,
                    data={"free": free, "fee": fee, "fee_modeled": modeled},
                    error=f"Insufficient free alpha: need {amount}, free {free}",
                )

            resp = subtensor.transfer_stake(
                wallet=bt_wallet,
                destination_coldkey_ss58=dest_coldkey,
                hotkey_ss58=hotkey,
                origin_netuid=netuid,
                destination_netuid=netuid,
                amount=amount,
            )
            # bittensor >=8 returns an ExtrinsicResponse (.success/.message); older
            # versions returned a bare bool. Accept either.
            ok = getattr(resp, "success", resp)
            if not ok:
                msg = (
                    getattr(resp, "message", None)
                    or getattr(resp, "error", None)
                    or "transfer_stake failed"
                )
                return ActionResult(ok=False, data={}, error=str(msg))

            return ActionResult(
                ok=True,
                data={"free": free, "fee": fee, "fee_modeled": modeled},
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
