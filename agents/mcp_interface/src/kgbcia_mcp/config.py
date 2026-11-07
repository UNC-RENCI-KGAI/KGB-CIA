"""Environment-backed configuration for the standalone server package."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


class Settings(BaseModel):
    """Runtime settings. Command-line values override matching environment values."""

    model_config = ConfigDict(frozen=True)

    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: str | None = None
    neo4j_database: str | None = None
    artifacts: str | None = None
    artifacts_sha256: str | None = None
    enable_cypher: bool = False

    @classmethod
    def from_env(
        cls,
        overrides: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Read supported variables, then apply non-None CLI-style overrides."""

        env = os.environ if environ is None else environ
        values: dict[str, Any] = {
            "neo4j_uri": env.get("NEO4J_URI"),
            "neo4j_user": env.get("NEO4J_USER"),
            "neo4j_password": env.get("NEO4J_PASSWORD"),
            "neo4j_database": env.get("NEO4J_DATABASE"),
            "artifacts": env.get("KGB_MCP_ARTIFACTS"),
            "artifacts_sha256": env.get("KGB_MCP_ARTIFACTS_SHA256"),
            "enable_cypher": _parse_bool(env.get("KGB_MCP_ENABLE_CYPHER", "0")),
        }
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(values)

    def require_neo4j(self) -> None:
        """Raise a useful error if a command needing Neo4j lacks connection settings."""

        missing = [
            name
            for name in ("neo4j_uri", "neo4j_user", "neo4j_password")
            if not getattr(self, name)
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"missing required Neo4j settings: {joined}")

    def require_artifacts(self) -> None:
        """Raise if a command needing lineage artifacts has no artifact source."""

        if not self.artifacts:
            raise ValueError("missing required setting: KGB_MCP_ARTIFACTS")


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")
