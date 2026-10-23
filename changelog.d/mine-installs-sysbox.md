### Changed
- `lium mine` installs Sysbox itself when Docker has no `sysbox-runc` runtime (as root or with passwordless sudo,
  like the rest of the install), so a new node needs no separate Sysbox step. A failed install stops with the manual
  command and the cause.
