"""``python -m earful`` is the ``earful`` command."""

import sys

from .cli import command

sys.exit(command())
