"""Fund command validation."""

import math


def validate_amount(amount_str: str) -> tuple[float, str]:
    """Validate a fund amount (TAO or alpha).

    Returns:
        (amount, error_message) - amount is 0.0 if invalid
    """
    try:
        amount = float(amount_str)
        # Reject non-finite values (inf passes a bare > 0 check) on a money path.
        if not math.isfinite(amount) or amount <= 0:
            return 0.0, "Amount must be a positive finite number"
        return amount, ""
    except ValueError:
        return 0.0, "Invalid amount format"


def validate_ss58(address: str, bt) -> tuple[str | None, str]:
    """Validate an SS58 address (e.g. the funding/destination coldkey).

    Delegates to bittensor's own checker rather than re-implementing SS58 decoding.

    Returns:
        (address, "") when valid, otherwise (None, error_message).
    """
    addr = (address or "").strip()
    if not addr:
        return None, "SS58 address is required"
    if not bt.utils.is_valid_ss58_address(addr):
        return None, f"invalid SS58 address: {addr}"
    return addr, ""


def resolve_hotkey(hotkey: str, wallet_name: str, bt) -> tuple[str | None, str]:
    """Resolve an origin hotkey given as either an SS58 address or a hotkey NAME.

    Mirrors btcli's ``stake move`` origin-hotkey handling: if the string is a valid
    SS58 it is used as-is; otherwise it is treated as a hotkey *name* under
    ``wallet_name`` and resolved to that hotkey's public SS58 (``hotkeypub`` first,
    falling back to ``hotkey`` for pre-3.1.1 wallets — same as btcli's
    ``get_hotkey_pub_ss58``). The hotkey SS58 is public, so no password is needed;
    the alpha transfer signs with the coldkey and uses the hotkey only as an
    identifier.

    Returns:
        (ss58, "") when resolved, otherwise (None, error_message).
    """
    raw = (hotkey or "").strip()
    if not raw:
        return None, "hotkey is required for --alpha (SS58 or wallet hotkey name)"
    if bt.utils.is_valid_ss58_address(raw):
        return raw, ""

    # Not an SS58: treat ``raw`` as a hotkey name under the coldkey wallet.
    # Construct the Wallet outside the swallow-all below — constructing is lazy
    # (touches no disk), so a missing keyfile only surfaces on the ss58 read. This
    # keeps API/programming errors (e.g. a renamed bittensor symbol) visible instead
    # of being mislabelled "hotkey doesn't exist".
    w = wallet_class(bt)(name=wallet_name, hotkey=raw)
    ss58 = None
    for attr in ("hotkeypub", "hotkey"):
        try:
            candidate = getattr(w, attr).ss58_address
        except Exception:
            # Missing/unreadable keyfile for this attr; try the next, then fall
            # through to the friendly not-found message below.
            continue
        if candidate:
            ss58 = candidate
            break

    if not ss58 or not bt.utils.is_valid_ss58_address(ss58):
        return None, (
            f"hotkey '{raw}' in wallet '{wallet_name}' is neither a valid SS58 "
            f"address nor a known hotkey name"
        )
    return ss58, ""


def wallet_class(bt):
    """Return the bittensor Wallet class across library versions.

    bittensor >=8 exposes ``bt.Wallet`` (the lowercase ``bt.wallet`` factory of older
    releases was removed); fall back to ``bt.wallet`` for those older versions.
    """
    cls = getattr(bt, "Wallet", None) or getattr(bt, "wallet", None)
    if cls is None:
        raise AttributeError("bittensor exposes neither 'Wallet' nor 'wallet'")
    return cls


def subtensor_class(bt):
    """Return the bittensor Subtensor class across library versions.

    bittensor >=8 exposes ``bt.Subtensor`` (the lowercase ``bt.subtensor`` factory of
    older releases was removed); fall back to ``bt.subtensor`` for those versions.
    """
    cls = getattr(bt, "Subtensor", None) or getattr(bt, "subtensor", None)
    if cls is None:
        raise AttributeError("bittensor exposes neither 'Subtensor' nor 'subtensor'")
    return cls
