# Requirements Document

## Introduction

Upgrade the `oscal-bindings` dependency from 0.1.0 to 0.3.1. Version 0.2.0 makes every model serialize to valid OSCAL JSON by default (resolves upstream cairn-proofs/oscal-bindings-python#5). Version 0.3.0 is breaking: about 95 classes are renamed, some names now refer to different types, closed enums reject out-of-enum values and hold Enum members, profile merge `combine`/`flat` are empty models, `MappingCollection.mappings` is always a `MappingCollectionMappings` RootModel, and union variants follow manifest order. Version 0.3.1 is generated from OSCAL 1.2.3, which matches the bundled schemas.

The upgrade must keep all MCP tool behavior unchanged, keep every validation path on dict input (upstream issue #6 makes JSON-string parsing drop Python-field-name keys on pydantic 2.13.x), and update the project's oscal-bindings guidance to reflect the new default serialization.

## Glossary

- **Server**: The `mcp-server-for-oscal` package source under `src/mcp_server_for_oscal/`.
- **Bindings**: The `oscal-bindings` Python library, imported as `oscal_bindings`.
- **Lock_File**: `requirements.txt`, produced by `hatch run update` and applied to every hatch environment through `UV_CONSTRAINT`.
- **Project_Manifest**: `pyproject.toml`.
- **MODEL_MAP**: The mapping in `tools/utils.py` from `OSCALModelType` to the Bindings top-level model class.
- **Store**: `OscalStore` in `tools/oscal_store.py`, including document validation and child element extraction.
- **Validator**: `tools/validate_oscal_content.py`, the four-level OSCAL validator.
- **Child_Element**: A record extracted by the Store from a parsed model (component, capability, control, group, import, modify, control-implementation, system-component, task, activity, result, finding, poam-item, mapping), with `raw_json`.
- **Valid_OSCAL_JSON**: JSON whose object keys are OSCAL property names (hyphenated aliases) and that contains no `null` values.
- **Test_Suite**: The `hatch run tests` script: mypy, `hatch test` on Python 3.13 and 3.14 with coverage, and bandit.
- **Guidance_Docs**: `.kiro/steering/oscal-bindings.md` and the matching serialization line in `AGENTS.md`.
- **Bundled_DB**: The gitignored `oscal_store.db` artifact built by `hatch run build-db`.

## Requirements

### Requirement 1: Dependency pin and lock

**User Story:** As a maintainer, I want the project pinned to oscal-bindings 0.3.1 with a re-locked dependency set, so that every environment uses the same, current Bindings release.

#### Acceptance Criteria

1. THE Project_Manifest SHALL declare the dependency `oscal-bindings==0.3.1`.
2. WHEN `hatch run update` completes, THE Lock_File SHALL pin `oscal-bindings==0.3.1`.
3. WHEN `hatch run update` completes, THE Lock_File SHALL contain only dependency changes that follow from the Bindings version change and the resolver output for the existing constraints.

### Requirement 2: Schema version alignment

**User Story:** As a maintainer, I want the Bindings schema version to match the bundled OSCAL schemas, so that model validation and JSON Schema validation agree.

#### Acceptance Criteria

1. THE Server SHALL bundle OSCAL schemas whose release version equals `oscal_bindings.__oscal_schema_version__`.
2. WHEN the Test_Suite runs, THE `TestBundledOscalVersion::test_matches_oscal_bindings_schema_version` test SHALL pass without changes to the bundled schemas.

### Requirement 3: Source compatibility with Bindings 0.3.x

**User Story:** As a developer, I want the Server code to use only names and shapes that exist in Bindings 0.3.1, so that the Server imports and type-checks cleanly.

#### Acceptance Criteria

1. THE Server SHALL import only Bindings names that exist in Bindings 0.3.1.
2. THE MODEL_MAP SHALL map each of the eight `OSCALModelType` members to the Bindings 0.3.1 top-level model class for that model type.
3. WHEN mypy runs as part of the Test_Suite, THE Server SHALL type-check with zero errors.
4. THE Server SHALL validate OSCAL documents by passing Python dict input to `model_validate` on the MODEL_MAP class.
5. THE Server SHALL contain zero calls to `model_validate_json` and zero calls to the Bindings `parse_*` helpers.
6. WHERE the Server compares a value from a closed-enum Bindings field with a string, THE Server SHALL compare the string with the Enum member value.

### Requirement 4: Behavior preservation

**User Story:** As an MCP client user, I want tool results to stay the same after the upgrade, so that agents relying on the tools see no regression.

#### Acceptance Criteria

1. WHEN the Store extracts Child_Elements from a document, THE Store SHALL produce the same set of (element_type, uuid, title) tuples as before the upgrade for every document in `tests/fixtures/` and `data/`.
2. WHEN the Store serializes a Child_Element, THE Store SHALL store `raw_json` as Valid_OSCAL_JSON.
3. WHEN a tool returns OSCAL content, THE Server SHALL return Valid_OSCAL_JSON.
4. WHEN the Store extracts mapping Child_Elements from a mapping-collection document, THE Store SHALL extract one Child_Element per entry in the `MappingCollectionMappings` root list.
5. WHEN the Store extracts controls from a catalog, THE Store SHALL extract controls nested at every group depth for groups of either Bindings catalog group class.
6. WHEN the Validator validates a document that a Bindings 0.3.1 model accepts, THE Validator SHALL report the model level as valid.
7. IF a document contains a value outside a closed OSCAL enum, THEN THE Validator SHALL report the model level as invalid with the Bindings validation error.
8. IF a document passed to the Store or Validator uses Python field names instead of OSCAL property names, THEN THE Store and Validator SHALL reject the document at the model level.

### Requirement 5: Test suite compatibility

**User Story:** As a maintainer, I want the full test suite and quality gates to pass on the new Bindings, so that the upgrade can merge with confidence.

#### Acceptance Criteria

1. WHEN `hatch run tests` runs, THE Test_Suite SHALL pass on Python 3.13 and Python 3.14.
2. WHEN `hatch check fmt` runs, THE Server and tests SHALL report zero formatting changes.
3. WHEN `hatch check code` runs, THE Server and tests SHALL report zero lint findings.
4. THE Hypothesis strategies in `tests/` that build OSCAL documents SHALL generate only values that Bindings 0.3.1 accepts for closed-enum fields, except in tests that target invalid input.
5. THE tests SHALL import only Bindings names that exist in Bindings 0.3.1.

### Requirement 6: Guidance documentation

**User Story:** As a developer or AI agent, I want the oscal-bindings guidance to describe the 0.2.0+ serialization behavior, so that I follow rules that are still justified.

#### Acceptance Criteria

1. THE Guidance_Docs SHALL state that Bindings models serialize to Valid_OSCAL_JSON by default since Bindings 0.2.0.
2. THE Guidance_Docs SHALL mark upstream issue cairn-proofs/oscal-bindings-python#5 as resolved in 0.2.0.
3. THE Guidance_Docs SHALL keep the rule against returning Pydantic models directly from tools, with a rationale that states the current justification.
4. THE Guidance_Docs SHALL keep the preference for returning stored Child_Element `raw_json`, with a rationale that states the current justification.
5. THE Guidance_Docs SHALL describe explicit `by_alias=True, exclude_none=True` arguments as optional and harmless instead of mandatory.
6. THE Guidance_Docs SHALL note upstream issue #6: on pydantic 2.13.x, JSON-string parsing silently drops keys spelled with Python field names, so validation uses dict input.
7. THE Guidance_Docs SHALL use catalog group and RootModel class names that exist in Bindings 0.3.1.
8. THE `AGENTS.md` serialization line SHALL state the same rule as `.kiro/steering/oscal-bindings.md` section 4.

### Requirement 7: Bundled database rebuild

**User Story:** As a maintainer, I want the bundled database rebuilt when parsing or extraction behavior changes, so that shipped content matches the new Bindings.

#### Acceptance Criteria

1. IF the upgrade changes Store extraction logic or Bindings parsing results for content in `data/`, THEN `hatch run build-db` SHALL complete with exit code 0.
2. WHEN `hatch run build-db` completes, THE Bundled_DB SHALL contain every document in `data/` that passed MODEL_MAP validation before the upgrade.
