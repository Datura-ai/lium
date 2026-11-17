### Security

- A pod id that is not a plain name (`.`, `..`, or one with a path character) now gets its own pin file under `~/.lium/known_hosts/`, named by its hash, so it never lands on the pin directory, its parent, or another pod's pin, and forgetting one pod's key never drops another's.
