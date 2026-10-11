### Fixed
- `lium up --ports N` is accepted as the only filter, as `up --help` shows; it was refused with "Must provide either NODE_ID or filters". `--ports` below 1 is refused (exit 2).
- `lium up` refuses `-e/--env`, `--entrypoint`, `--cmd` and `--internal-ports` without `--image` (exit 2). A template rent dropped them without a word and started the pod without them.
- `lium up --image` keeps a registry port in the image name (`localhost:5000/team/img` is image `localhost:5000/team/img`, tag `latest`), and passes a digest reference (`img@sha256:…`) through whole so the pod runs exactly that image.
- `lium ls` with `--count`, `--min-cuda`, `--min-cpus` or `--max-distance` that matches no node says `No available node matches <flags>` instead of "All GPUs are currently rented out"; `--count` below 1 is refused (exit 2) instead of listing every node.
- `lium reboot` names the pods it rebooted, also when others in the batch failed, and a failure carries each pod's reason (`<huid> (<error>)`) instead of the HUID alone.
- `lium provider portal whoami` with no saved session says `no portal session for this hotkey` instead of "token rejected", and `lium provider status` prints `portal=not-signed-in` for that case instead of `portal=down`.
- `lium provider --help` no longer calls `lium mine` a renter workflow or claims `provider` installs nodes and reports weights.
