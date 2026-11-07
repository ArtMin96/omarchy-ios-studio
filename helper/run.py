"""Start a module of the iosstudio package: `python3 -I helper/run.py <module> [args...]`.

`python -I` leaves the script's folder off sys.path, so this puts helper/ there and runs <module> (`iosstudio` for
the helper, `iosstudio.device.real.daemon` for the device daemon, ...) as __main__ with sys.argv[1:] = args.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)
