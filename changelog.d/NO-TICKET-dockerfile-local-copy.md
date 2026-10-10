### Fixed
- `lium up --dockerfile` refuses a Dockerfile whose `COPY`/`ADD` reads local files before renting, and lists those lines. The node builds with the Dockerfile as its only file, so such a build used to fail with `not found` after the pod was rented. `COPY --from=<stage>`, heredoc sources and `ADD <url>` are unaffected.
