### Fixed
- SDK reference: `LiumRentOutcomeUnknownError` opens with "check `ps()` before renting again" and lists every case that raises it; `LiumInsufficientBalanceError` names only the 402 the API sends (`BALANCE_REQUIRED`), with `account_unfunded` still recognised if a server sends it.
