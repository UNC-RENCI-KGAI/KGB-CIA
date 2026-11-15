"""Transformation report models.

These models intentionally mirror ``shared.schema`` but cannot import it: the published server
must install and run without the repository. A repository test compares their fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ColumnRef(BaseModel):
    field: str = Field(description="The cr:Field @id")
    name: str = Field(description="Column name in source.extract.column")


class Mapping(BaseModel):
    id: str = Field(description="Unique mapping identifier within the report")
    file: str
    record_set: str | None = None
    plan_layout: str | None = None
    produces: Literal["node", "edge"]
    category: str | None = None
    subject_category: str | None = None
    predicate: str | None = None
    object_category: str | None = None
    columns: dict[str, list[ColumnRef]] = Field(default_factory=dict)
    constants: dict[str, str | float | int | bool] = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    code: str | None = None
    rows_read: int | None = None
    records_written: int | None = None


class Skipped(BaseModel):
    ref: str
    name: str | None = None
    reason: str


class TransformReport(BaseModel):
    source: str
    source_version: str | None = None
    croissant: str
    semantic_croissant: str | None = None
    mappings: list[Mapping] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
