---
inclusion: always
---

# OSCAL Bindings Guidelines

## Overview

This project uses `oscal-bindings` (imported as `oscal_bindings`) for OSCAL models, validation, and serialization. It replaces compliance-trestle. Prefer the library's capabilities over custom implementations.

## Core Rules

### 1. Typed models only
- Use the typed classes in `oscal_bindings.models` (`Catalog`, `Profile`, `ComponentDefinition`, `SystemSecurityPlan`, `AssessmentPlan`, `AssessmentResults`, `PlanOfActionAndMilestones`, `MappingCollection`)
- Get the class for a model type from `MODEL_MAP` in `tools/utils.py`, the single source of model classes. Don't import or map classes elsewhere
- Parse the object under the root key: `MODEL_MAP[model_type].model_validate(doc[root_key])`
- Never use `parse_oscal()`, `parse_oscal_file()`, or the other `parse_*` helpers; they return union/wrapper types instead of the mapped class

### 2. Validation
- Model validation is strict: models forbid extra fields, so unknown properties fail
- Use `model_validate` instead of writing custom OSCAL validation logic

### 3. Data shapes
- Datetimes keep their timezone offset; don't normalize them
- Array-valued fields can be `RootModel` wrappers (e.g. mapping-collection `mappings`). Unwrap with `getattr(x, "root", x)` before treating them as a list
- Catalog groups are two classes: `CatalogGroupWithControls` and `CatalogGroupWithGroups`. Handle both

### 4. Serialization and tool output
- Tool output must be valid OSCAL JSON: hyphenated property names, no nulls
- Never return Pydantic models directly from tools. The MCP SDK and Strands don't apply `exclude_none`, and Strands falls back to Python `repr` for models nested in dicts
- Prefer returning stored element JSON (child `raw_json`) over re-serializing models
- When you must serialize, always pass both flags explicitly: `model_dump(mode="json", by_alias=True, exclude_none=True)` or `model_dump_json(by_alias=True, exclude_none=True)`

### 5. Schema version
- `oscal_bindings.__oscal_schema_version__` must match the bundled schemas in `oscal_schemas/`. A guard test in `tests/test_utils.py` enforces this
- Bumping the bundled schemas requires a matching `oscal-bindings` release

## Implementation Workflow

1. Before writing OSCAL code, check `oscal_bindings` and `tools/utils.py` for existing functionality
2. If the library lacks a capability, request it upstream at https://github.com/cairn-proofs/oscal-bindings-python (e.g. cairn-proofs/oscal-bindings-python#5, serialize to valid OSCAL by default) rather than building a local workaround
3. If a local workaround is unavoidable, document what you checked, why the library doesn't fit, and link the upstream issue
4. When uncertain, ask before implementing

## Anti-Patterns to Avoid

- Manually parsing or walking OSCAL JSON when a typed model fits
- Custom OSCAL validation instead of `model_validate`
- Custom OSCAL type definitions instead of `oscal_bindings.models`
- Union `parse_*` helpers instead of `MODEL_MAP` classes
- Returning models, or serializing without `by_alias=True, exclude_none=True`

## Example

```python
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from pydantic import ValidationError

model_type = OSCALModelType.COMPONENT_DEFINITION
try:
    cdef = MODEL_MAP[model_type].model_validate(doc[model_type.value])
except ValidationError as e:
    ...  # report via try_notify_client_error, then raise or return an error dict

json_str = cdef.model_dump_json(by_alias=True, exclude_none=True, indent=2)
```
