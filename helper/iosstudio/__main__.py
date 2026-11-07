"""`python -m iosstudio <group> <action> [--flags]`: the helper (iosstudio.cli)."""
import sys

from iosstudio.cli import main

sys.exit(main(sys.argv[1:]))
