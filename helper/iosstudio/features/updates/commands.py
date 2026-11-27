"""Group `updates`: toolchain updates on the tested or latest channel (docs/features/toolchain-updates.md).

updates status                         one JSON object: cached upstream facts + installed versions (files only, fast)
updates check [--force]                JSON lines: component events, done {ok, data: status}; at most once a day
updates apply (--component ID | --all) [--channel tested|latest] [--dry-run] [--yes]
                                       JSON lines: Setup's step/progress/log/prompt events + verified/rollback;
                                       done {applied, rolledBack, status}. Without --component/--all it only
                                       puts back what an interrupted update left set aside.
updates channel --set tested|latest    one JSON object: {channel, reinstall: [ids an `apply --all` would install]}
"""
from __future__ import annotations

from iosstudio.core.output import HelperError, ok

from ..setup import channel as ch
from ..setup.runtime import streaming
from . import model


def cmd_status(flags: dict) -> int:
    return ok(model.status())


@streaming
def cmd_check(flags: dict) -> int:
    from . import check
    return check.run(bool(flags.get("force")))


@streaming
def cmd_apply(flags: dict) -> int:
    from . import apply
    return apply.apply(flags)


def cmd_channel(flags: dict) -> int:
    c = flags.get("set")
    if not isinstance(c, str):
        raise HelperError("usage", "usage: updates channel --set tested|latest")
    ch.set_channel(c)
    st = model.status(c)
    return ok({"channel": c, "reinstall": [x["id"] for x in st["components"] if x["state"] == "update"]})


GROUPS = {"updates": {"status": cmd_status, "check": cmd_check, "apply": cmd_apply, "channel": cmd_channel}}
