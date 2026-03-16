If you're doing fix on an LTS branch (like `v6.4.x` or similar) and are asked
to port something to `master`, make the port based purely on `master` code; do
not attempt to make a patch that is backwards compatible with older LTS
branches. The patch should focus on the state of the codebase as it is in the
target branch (`master` in this case).
