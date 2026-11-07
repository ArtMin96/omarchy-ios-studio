"""A fake `xtool` for the account slice (set IOSSTUDIO_XTOOL to it, FAKE_XTOOL_DIR to a scenario folder).

Scenario folder files (all optional):
  status            what `xtool auth status` prints (default: signed in as jane@example.com, team TEAM123456)
  <section>.txt     what `xtool ds <section> list` prints; <section>.rc its exit code (default 0;
                    a missing .txt prints "Error: server busy (503) get_headers" and exits 1)
  teams             makes `auth login` ask for a team after the password
  calls             appended: one line per call (argv), and each login answer as `answer:<text>`
`auth login`: Password: -> [team choice] -> Code: ; code 123456 signs in, anything else fails like Apple does.
"""
from __future__ import annotations

FAKE_XTOOL = r'''#!/usr/bin/env python3
import os, sys
d = os.environ["FAKE_XTOOL_DIR"]
args = sys.argv[1:]
def note(s):
    with open(os.path.join(d, "calls"), "a") as f:
        f.write(s + "\n")
def read(name, default=None):
    p = os.path.join(d, name)
    return open(p).read() if os.path.exists(p) else default
def ask(prompt):
    sys.stdout.write(prompt); sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        sys.exit(9)
    note("answer:" + line.rstrip("\n"))
    return line.strip()
note(" ".join(args))
if args[:2] == ["auth", "status"]:
    sys.stdout.write(read("status", "Logged in.\n - Apple ID: jane@example.com\n - Team ID: TEAM123456\n"
                                    " - Token expiry: 31/10/2099, 2:33 PM\n"))
elif args[:2] == ["auth", "logout"]:
    print("Logged out")
elif args[:1] == ["ds"]:
    text = read(args[1] + ".txt")
    if text is None:
        print("Error: server busy (503) get_headers"); sys.exit(1)
    sys.stdout.write(text)
    sys.exit(int(read(args[1] + ".rc", "0")))
elif args[:2] == ["auth", "login"]:
    ask("Password: ")
    if read("teams") is not None:
        print("Teams:")
        print("  1: Jane Doe (TEAM123456)")
        print("  2: Acme Inc (ACME000001)")
        ask("Choice (1-2): ")
    code = ask("Code: ")
    print("Logging in...")
    if code == "123456":
        print("Logged in.")
        sys.exit(0)
    print("Error: IncorrectVerificationCode (-21669)")
    sys.exit(1)
else:
    print("Error: unknown command"); sys.exit(2)
'''
