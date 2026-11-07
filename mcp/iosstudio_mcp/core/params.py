"""Parameter types more than one tool takes: one definition (and one description) each.

A parameter only one module uses stays in that module.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from . import budget as B
from .registry import MAX_WAIT

Udid = Annotated[str | None, Field(description="iPhone udid (default: the active iPhone)")]
BundleId = Annotated[str, Field(description="e.g. 'com.example.myapp' (the installed XTL-<TEAM>. form works too)")]
ProjectPath = Annotated[str, Field(description="Project folder (absolute or ~/…) or name from ios_project_list")]
MaxBytes = Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Result size cap in bytes (default "
                                                                         "12000)")]
WaitSeconds = Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: return when done or after this many "
                                                                  "seconds")]
PresetName = Annotated[str | None, Field(description="Run preset of the app's project (ios_project_presets), e.g. "
                                                     "'German'; its args come first, then `args`")]
JobId = Annotated[str, Field(description="Job id from a tool that started a job (e.g. 'b-1')")]
OptJobId = Annotated[str | None, Field(description="Wait for a job this tool started earlier instead of starting one")]
