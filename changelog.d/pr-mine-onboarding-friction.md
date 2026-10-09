### Fixed
- CLI: `lium mine` stops at step 3 when the host's disk cannot pass validation (100 GiB must stay free after about 40 GiB of node images), instead of failing at step 6 after the image pulls.
- CLI: `lium mine` prints the "Register this node" link and the `lium provider node add` command without wrapping, so a click no longer opens Add Node with half an IP address.
- CLI: `lium mine`'s closing note says the opt-in step applies only when the account is not opted in yet, and that `--price` must stay inside the range the portal shows for the model.
