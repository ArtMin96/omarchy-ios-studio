"""iOS Studio's helper and device daemon (stdlib only; the real daemon also uses pymobiledevice3).

core/      output protocol, subprocesses, files, app folders, flags: shared by everything
features/  the helper's commands (`iosstudio-helper <group> <action>`), one module per feature
device/    the device daemon (real and demo), its broker and client, and their feature modules
demo/      the helper's demo mode (made-up data for screenshots and walkthroughs)
Start it with helper/run.py (bin/iosstudio-helper, bin/iosstudio-deviced). See docs/development.md.
"""
