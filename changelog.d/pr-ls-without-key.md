### Changed
- CLI: `lium ls` and `lium templates` work before you have an account. With no API key they read the public listings (the same nodes, prices and templates) and end with how to sign up; every command that acts on an account still stops with `no_api_key`.
