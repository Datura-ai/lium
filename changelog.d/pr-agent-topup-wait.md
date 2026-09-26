### Added
- `lium topup link -a USD`: a Stripe Checkout page a person pays by card, for an agent with no card or browser; nothing is charged by the command. `--json` adds `balance_before` and a `handoff` object.
- `lium topup wait [--above USD] [--timeout S]`, and `--wait SECONDS` on `topup link`, `topup create` and `topup card`: wait until the webhook credits the balance and report `seconds_to_credit`; exit 3 with `credit_not_seen` on timeout. Under `--json` the page or invoice goes to stderr first.
- `lium signup --billing-key`: also mint a key holding only the `billing` scope, kept as `[api] billing_api_key`; money commands use it (or `LIUM_BILLING_API_KEY`) before the usual key.
- SDK: `Lium.topup_checkout_link()` and `Lium.wait_for_credit()`.
