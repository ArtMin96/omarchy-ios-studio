"""Parameter types more than one tool takes: one definition (and one description) each.

A parameter only one module uses stays in that module.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from . import budget as B
from .registry import MAX_WAIT

Udid = Annotated[str | None, Field(description="iPhone udid; default: the active iPhone (see ios_device_list)")]
BundleId = Annotated[str, Field(description="Bundle id, e.g. 'com.example.myapp'. The installed "
                                            "'XTL-<TEAM>.com.example.myapp' form also works")]
ProjectPath = Annotated[str, Field(description="Project folder (absolute or ~/…) or a project name from "
                                               "ios_project_list, e.g. '~/Developer/WeatherGlance' or 'WeatherGlance'")]
MaxBytes = Annotated[int | None, Field(ge=1000, le=B.MAX_BYTES, description="Raise the size cap for text + "
                                                                         "structuredContent together (default 12000 bytes)")]
WaitSeconds = Annotated[int, Field(ge=0, le=MAX_WAIT, description="Long-poll: return as soon as the job finishes "
                                                                  "or needs input, or after this many seconds")]
PresetName = Annotated[str | None, Field(description="A run preset saved for the app's project (see "
                                                     "ios_project_presets), e.g. 'German'. Its launch arguments come "
                                                     "first, then `args`; `env` overrides its environment")]
