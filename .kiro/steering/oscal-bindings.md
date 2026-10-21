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
- Validate with `model_validate(dict)` (`json.loads` first). Don't use `model_validate_json` on user content: upstream cairn-proofs/oscal-bindings-python#6 makes the JSON path silently drop keys spelled with Python field names (e.g. `document_ids`), while the dict path rejects them. Reported on pydantic 2.13.x; still reproduces on the project's pydantic 2.14.1

### 3. Data shapes
- Datetimes keep their timezone offset; don't normalize them
- Array-valued fields can be `RootModel` wrappers (e.g. `MappingCollectionMappings` for mapping-collection `mappings`). Unwrap with `getattr(x, "root", x)` before treating them as a list
- Catalog groups are two classes: `CatalogGroupWithControls` and `CatalogGroupWithGroups` (union alias `CatalogGroup`). Handle both
- Closed-enum fields hold `Enum` members, not strings. Compare with `.value` (or the member); `str()` on a member doesn't return the OSCAL value

### 4. Serialization and tool output
- Tool output must be valid OSCAL JSON: hyphenated property names, no nulls
- Since oscal-bindings 0.2.0, models serialize to valid OSCAL by default: `model_dump`, `model_dump_json`, and `pydantic_core.to_json` (including models nested in plain containers) use aliases and omit nulls. `exclude_none=False` has no effect. Explicit `by_alias=True, exclude_none=True` is optional and harmless
- Prefer returning stored element JSON (child `raw_json`) over re-serializing models. It avoids re-parsing and re-serializing on every request and returns exactly what was indexed
- Never return Pydantic models directly from tools; return plain dicts/strings. The MCP SDK and Strands serialize results through their own paths (Strands can fall back to `repr` for non-JSON-serializable values in dicts). 0.2.0 fixes what Pydantic emits, but not callers that bypass Pydantic serialization (e.g. `json.dumps(..., default=str)`)

### 5. Schema version
- `oscal_bindings.__oscal_schema_version__` must match the bundled schemas in `oscal_schemas/`. A guard test in `tests/test_utils.py` enforces this
- Bumping the bundled schemas requires a matching `oscal-bindings` release

## Implementation Workflow

1. Before writing OSCAL code, check `oscal_bindings` and `tools/utils.py` for existing functionality
2. If the library lacks a capability, request it upstream at https://github.com/cairn-proofs/oscal-bindings-python (e.g. cairn-proofs/oscal-bindings-python#5, serialize to valid OSCAL by default, resolved in 0.2.0) rather than building a local workaround
3. If a local workaround is unavoidable, document what you checked, why the library doesn't fit, and link the upstream issue
4. When uncertain, ask before implementing

## Anti-Patterns to Avoid

- Manually parsing or walking OSCAL JSON when a typed model fits
- Custom OSCAL validation instead of `model_validate`
- Custom OSCAL type definitions instead of `oscal_bindings.models`
- Union `parse_*` helpers instead of `MODEL_MAP` classes
- Returning Pydantic models from tools
- `model_validate_json` on untrusted content (see upstream #6)

## Example

```python
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from pydantic import ValidationError

model_type = OSCALModelType.COMPONENT_DEFINITION
try:
    cdef = MODEL_MAP[model_type].model_validate(doc[model_type.value])
except ValidationError as e:
    ...  # report via try_notify_client_error, then raise or return an error dict

# by_alias/exclude_none are the 0.2.0+ defaults; kept explicit for clarity
json_str = cdef.model_dump_json(by_alias=True, exclude_none=True, indent=2)
```
