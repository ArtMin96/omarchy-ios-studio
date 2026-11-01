"""Demo: ddi.status, ddi.mount, devmode.status, devmode.reveal."""
from __future__ import annotations


def register(reg):
    reg.command("ddi.status", lambda ctx, a: {"mounted": True})
    reg.command("ddi.mount", lambda ctx, a: {"mounted": True})
    reg.command("devmode.status", lambda ctx, a: {"enabled": True})
    reg.command("devmode.reveal", lambda ctx, a: {})
