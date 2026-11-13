### Fixed
- `lium mine` step 3 (Checking prerequisites) can no longer hang without a word: the spinner names the check that is running (nvidia-smi, docker info, the Sysbox install, Docker storage), each check is stopped at its time limit with the command named, and when another apt/dpkg process holds the package lock the step stops before the Sysbox install and shows that process.
