import time
from typing import Any

from lium.cli.actions import ActionResult


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
            bt_wallet = bt.wallet(wallet_name)
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

            if needs_registration:
                lium.add_wallet(bt_wallet)
                time.sleep(2)  # Allow registration to complete

            return ActionResult(
                ok=True,
                data={"registered": not needs_registration}
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class ExecuteTransferAction:
    """Execute TAO transfer to Lium funding address."""

    LIUM_FUNDING_ADDRESS = "5FqACMtcegZxxopgu1g7TgyrnyD8skurr9QDPLPhxNQzsThe"

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
            subtensor = bt.subtensor()

            success = subtensor.transfer(
                wallet=bt_wallet,
                dest=self.LIUM_FUNDING_ADDRESS,
                amount=bt.Balance.from_tao(tao_amount)
            )

            if not success:
                return ActionResult(ok=False, data={}, error="Transfer failed")

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
