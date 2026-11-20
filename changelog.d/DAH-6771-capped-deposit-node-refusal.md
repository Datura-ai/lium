### Fixed
- `lium fund --gamma` stops before signing when the deposit's daily spend cap allows less than the payment needs, and names the exact amount it needs. Removing the cap lets the payment run at once; a raised cap first has to refill; before, the chain refused the spend and reverted the whole payment.
- `lium fund --gamma` on bittensor 10: a payment the node refuses (for example `1010 Invalid Transaction: Inability to pay some fees`) no longer holds the next run for about 26 minutes. A definitive node refusal clears the pending payment record. If the outcome is uncertain, the CLI waits for the payment to settle.
- `lium fund --gamma` runs one payment per wallet at a time; a second run from the same wallet stops until the first has finished.
- `lium fund --gamma` stops before signing if the chain's runtime changes while the command runs; run it again.
