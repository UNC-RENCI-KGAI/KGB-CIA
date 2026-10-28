"""Version 1 of the generated-parser interface; final graph models live in shared.schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PARSER_API_VERSION = 1


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file: str = Field(min_length=1)
    record_set: str = Field(min_length=1)
    record: str | int


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["info", "warning", "error"] = "warning"
    skipped: bool = False


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["node", "edge", "diagnostic"]
    data: dict
    location: Location
