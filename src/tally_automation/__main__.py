"""Entry point for ``python -m tally_automation`` (server, or ``users …`` management)."""

import sys

from tally_automation.cli import main

if __name__ == "__main__":
    sys.exit(main())
