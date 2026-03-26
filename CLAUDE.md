If you're doing fix on an LTS branch (like `v6.4.x` or similar) and are asked
to port something to `master`, make the port based purely on `master` code; do
not attempt to make a patch that is backwards compatible with older LTS
branches. The patch should focus on the state of the codebase as it is in the
target branch (`master` in this case).

## Commit and PR attribution

Every commit message and every PR description MUST end with:

    Sponsored-By: Netflix

If a `Co-Authored-By` trailer is present, `Sponsored-By` MUST follow it
directly with NO blank line between them:

    Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
    Sponsored-By: Netflix

## Copyright

Every new file MUST have the standard Xpra copyright header at the very top,
before any ABOUTME lines, in the comment format for that language, with the
Netflix copyright and the year updated to match the current year. Example for
Python (shebang line first if present, then copyright, then ABOUTME):

    #!/usr/bin/env python3
    # This file is part of Xpra.
    # Copyright (C) <current year> Netflix, Inc.
    # Xpra is released under the terms of the GNU GPL v2, or, at your option, any
    # later version. See the file COPYING for details.
    # ABOUTME: ...

For existing files with significant changes, add a Netflix copyright line to
the existing author list. If unsure whether changes are significant enough,
ask. ALWAYS check this before opening a PR.
