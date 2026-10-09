### Added
- `lium mine --ip <IPv4> --internal-port <N> --external-port <N>`: register a node behind CGNAT or a port-forwarding VPS at the address and port validators reach. `--ip` replaces the auto-detected address in the printed `lium provider node add` line and in `--register`; the ports also work with `--auto` (and the `mine.sh` one-liner), and `--external-port` defaults to `--internal-port`.
