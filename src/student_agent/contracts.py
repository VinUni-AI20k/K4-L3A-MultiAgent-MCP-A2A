from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from . import VARIANT_ID

PUBLIC_SCHEMA_FILES = frozenset(
    {
        "l3a-output-v2.schema.json",
        "trace-event-v1.schema.json",
        "submission-manifest-v2.schema.json",
        "mcp-evidence-response-v1.schema.json",
    }
)


class ContractError(ValueError):
    pass


class Contracts:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        for path in sorted(self.root.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema
            resource = Resource.from_contents(schema)
            registry = registry.with_resource(schema["$id"], resource)
        missing = sorted(PUBLIC_SCHEMA_FILES - schemas.keys())
        if missing:
            raise ContractError(f"missing public contract schema(s): {', '.join(missing)}")
        self._schemas = schemas
        self._registry = registry

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise ContractError(f"contract not found: {schema_name}")
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ContractError(f"{label}:{location}: {error.message}")

    def validate_output(self, value: Any, label: str) -> None:
        self.validate(f"{VARIANT_ID}-output-v2.schema.json", value, label)

    def validate_trace(self, value: Any, label: str) -> None:
        self.validate("trace-event-v1.schema.json", value, label)

    def validate_manifest(self, value: Any, label: str = "manifest.json") -> None:
        self.validate("submission-manifest-v2.schema.json", value, label)

    def validate_evidence(self, value: Any, label: str = "MCP response") -> None:
        self.validate("mcp-evidence-response-v1.schema.json", value, label)
