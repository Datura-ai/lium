### Added
- SDK: `Lium.up(wait=True, verify_gpus=True)` checks the billed GPU count and the GPUs visible inside a pod before returning it. Verification errors identify the still-billing pod for cleanup.
