### Fixed
- `lium provider node get` and the provider tables no longer report a paying port-limited remainder as blocking or
  exit 10; they print "Partial idle pay" with the measured and required free ports.
