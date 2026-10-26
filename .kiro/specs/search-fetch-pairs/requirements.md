# Requirements Document

## Introduction

GitHub issue #45. `text_search_oscal` and `query_oscal_documentation` should form two discover → fetch pairs:

| Content | Discover (snippets) | Fetch (full) |
|---|---|---|
| OSCAL entities | `text_search_oscal` | `get_child_element` (child elements), the per-model query tools (documents) |
| Documentation | `query_oscal_documentation` | `get_oscal_documentation` (new) |

Neither pair works end to end today. `text_search_oscal` returns the internal FTS row id as `entity_id`, which `get_child_element` cannot resolve; it returns whole documentation pages (up to ~31 KB each); catalog controls are indexed by title only; hyphenated terms (`back-matter`, `ac-2`) raise `sqlite3.OperationalError` and fall back to an unranked `LIKE`; and documents from `OSCAL_DOCUMENTS_DIR` stay invisible until another tool indexes them. `query_oscal_documentation` cannot reach user documentation when a Bedrock Knowledge Base is configured, ANDs every term of a question, has no paging, and returns a different shape in KB and local modes.

Several tool results also disclose filesystem paths and storage locations: `query_oscal_documentation` returns `source` (an absolute path) in local mode and the raw Bedrock `retrieve` response (with S3 `location` and metadata) in KB mode, and `query_<model>` items carry `file_path`. Documentation IDs are `uuid5(NAMESPACE_URL, <absolute path>)`, so they differ per machine.

This feature is non-breaking: no tool is renamed or removed, and documentation stays out of the OSCAL model-type enums that #40 will consolidate. Facts above were checked in `oscal_store.py`, `query_oscal_models.py`, `query_documentation.py`, `validate_oscal_content.py`, and `main._init_oscal_store()`; the KB response shape comes from the Bedrock `retrieve` API, not a live call.

## Glossary

- **Server**: The `mcp-server-for-oscal` MCP server process started by `main.main()`.
- **OscalStore**: The SQLite-backed store in `tools/oscal_store.py` (`documents`, `child_elements`, and the FTS5 `fts_index` tables).
- **Tool_Registry**: `get_tool_list()` in `tools/__init__.py`, the single list of tools used by the Server, the agent, and the `.mcpb` manifest.
- **Text_Search_Tool**: The `text_search_oscal` MCP tool and the `OscalStore.text_search` method behind it.
- **Documentation_Search_Tool**: The `query_oscal_documentation` MCP tool and the `OscalStore.search_documentation` method behind it.
- **Doc_Fetch_Tool**: The new `get_oscal_documentation` MCP tool that returns the text of one Documentation_Document by Doc_ID.
- **Child_Fetch_Tool**: The existing `get_child_element` MCP tool.
- **Per_Model_Query_Tools**: The existing `query_catalog`, `query_profile`, `query_ssp`, `query_assessment_plan`, `query_assessment_results`, `query_poam`, `query_mapping_collection`, and `query_component_definition` tools.
- **OSCAL_Document**: A `documents` row whose `model_type` is an `OSCALModelType` value.
- **Documentation_Document**: A `documents` row whose `model_type` is `"documentation"` (a markdown file ingested by `scan_directory`).
- **Child_Element**: A `child_elements` row extracted from an OSCAL_Document.
- **Element_ID**: The `uuid` column of a Child_Element: a UUID, or a token ID such as `ac-2` or `import-0`.
- **Document_UUID**: The `uuid` column of an OSCAL_Document.
- **Scan_Root**: The directory passed to one `OscalStore.scan_directory` call.
- **Origin**: The provenance recorded for each `documents` row at ingestion: `bundled` for content scanned by `bin/build_oscal_db.py` into the bundled DB, `user` for content scanned from `OSCAL_DOCUMENTS_DIR` at Server startup.
- **Relative_Name**: The path of an ingested file relative to its Scan_Root, with `/` separators; for a zip member, the zip file's Relative_Name followed by `/` and the member name.
- **Doc_ID**: The identifier of a Documentation_Document: a UUID version 5 derived from the document's Origin and Relative_Name.
- **KB_Mode**: The Documentation_Search_Tool state when `config.knowledge_base_id` is non-blank (a Bedrock Knowledge Base is configured).
- **Local_Mode**: The Documentation_Search_Tool state when `config.knowledge_base_id` is blank.
- **KB_Hit**: One result of a Bedrock Knowledge Base `retrieve` call.
- **Local_Hit**: One Documentation_Document matched by the OscalStore documentation search.
- **Doc_Result_Item**: One item in a Documentation_Search_Tool page: `title` (string), `snippet` (string), and `docId` (string, present only for a Local_Hit).
- **Search_Term**: One token produced by Query_Normalization. A token that contains a hyphen or other non-alphanumeric joiner (e.g. `back-matter`, `ac-2`) is one Search_Term matched as an FTS5 phrase.
- **Stop_Word**: A word in a fixed, documented list of English function words (e.g. `how`, `does`, `the`, `what`, `is`) that Query_Normalization drops.
- **Query_Normalization**: The conversion of caller query text into Search_Terms that are escaped so that no caller character is interpreted as FTS5 syntax.
- **Storage_Location**: Any of: an absolute filesystem path; the OscalStore DB path; the `OSCAL_DOCUMENTS_DIR` path; a Scan_Root path; a temporary directory path; the user's home directory path; the installed package directory path; an `s3://` URI; Bedrock KB `location` or `metadata` values.
- **Metaschema_Source_File**: A bundle-relative file name listed in the Metaschema manifest allowlist (`MetaschemaIndex.source_files()`), returned as `source-file` by the Metaschema tools and accepted by `get_metaschema_source`. A Metaschema_Source_File is a logical identifier, not a Storage_Location.
- **Client_Facing_Message**: Any message passed to `try_notify_client_error`, the text of any exception a tool raises to the caller, and any error string inside a tool result.
- **Path_Leak_Test**: The test that runs every tool in the Tool_Registry against the Fixture_Store and checks results and Client_Facing_Messages for Storage_Locations.
- **Fixture_Store**: An OscalStore built in a test temporary directory from fixture files that cover every `OSCALModelType` and at least one Documentation_Document of each Origin.
- **Page_Response**: The envelope from `utils.paginate` or `utils.paginate_within_budget`: `items`, `total`, `offset`, `limit`, `hasMore`.
- **RESPONSE_BUDGET**: `utils.RESPONSE_BUDGET`, the maximum serialized JSON length of a budgeted tool response.
- **TRUNCATION_MARK**: `utils.TRUNCATION_MARK`, the marker appended to shortened text.
- **Control_Prose**: The text of a catalog control's `statement` and `guidance` parts, including the prose of their nested parts, in document order.
- **TOTAL_BUDGET**: `tool_budget.TOTAL_BUDGET` (38,979 characters), the maximum compact JSON size of the `tools/list` `tools` array.
- **Sync_Targets**: The files listed under "Sync_Targets" in `.kiro/steering/tool-authoring.md`.

## Requirements

### Requirement 1: Record document Origin at ingestion

**User Story:** As a maintainer, I want each stored document to record whether it came from the bundled DB or from `OSCAL_DOCUMENTS_DIR`, so that tools can treat user documents separately without matching on filesystem paths.

#### Acceptance Criteria

1. THE OscalStore SHALL record exactly one Origin, `bundled` or `user`, for every `documents` row.
2. WHEN `OscalStore.scan_directory` is called, THE OscalStore SHALL accept the Origin for that Scan_Root as an argument and record that Origin on every row the call ingests.
3. WHEN `bin/build_oscal_db.py` scans `data/component_definitions` and `data/oscal_docs`, THE build script SHALL record Origin `bundled`.
4. WHEN `main._init_oscal_store()` scans `OSCAL_DOCUMENTS_DIR`, THE Server SHALL record Origin `user`.
5. WHEN `scan_directory` encounters a file whose content hash is unchanged since a previous ingestion, THE OscalStore SHALL update the row's Origin and Relative_Name to the values of the current call.
6. THE OscalStore SHALL select documents by Origin through the recorded Origin value, not through a `file_path` prefix comparison.
7. WHEN the Server reports the number of documents loaded from `OSCAL_DOCUMENTS_DIR` (the `about` tool), THE Server SHALL count `documents` rows with Origin `user`.

### Requirement 2: Stable, path-free Documentation IDs

**User Story:** As an agent, I want documentation IDs that are the same on every machine and do not encode a path, so that I can cite and fetch a document reliably.

#### Acceptance Criteria

1. WHEN the OscalStore ingests a markdown file, THE OscalStore SHALL set the Doc_ID to a UUID version 5 computed from the file's Origin and Relative_Name.
2. THE OscalStore SHALL compute the same Doc_ID for the same Origin and Relative_Name regardless of the absolute location of the Scan_Root, the operating system path separator, or the machine.
3. WHERE a bundled Documentation_Document and a user Documentation_Document have the same Relative_Name, THE OscalStore SHALL assign them different Doc_IDs.
4. WHEN `scan_directory` re-ingests a markdown file whose content hash is unchanged and whose stored Doc_ID was derived from an absolute path, THE OscalStore SHALL replace the stored Doc_ID with the Doc_ID from criterion 1.
5. THE OscalStore SHALL keep the OSCAL `uuid` from the document content as the Document_UUID of each OSCAL_Document.

### Requirement 3: Path-free tool results

**User Story:** As a security reviewer, I want no tool result to contain a filesystem path or storage location, so that results do not disclose usernames, directory layout, or deployment details.

#### Acceptance Criteria

1. THE Server SHALL return tool results that contain no Storage_Location.
2. THE Per_Model_Query_Tools SHALL return document items without a `file_path` key.
3. THE Documentation_Search_Tool SHALL return Doc_Result_Items without a `source` key.
4. WHILE in KB_Mode, THE Documentation_Search_Tool SHALL return Doc_Result_Items built from KB_Hits that contain no KB `location`, no KB `metadata`, no `s3://` URI, and no Bedrock response metadata.
5. THE Metaschema tools (`get_allowed_values`, `get_constraints`, `get_definition`, `find_references_to`, `get_metaschema_source`) SHALL keep returning Metaschema_Source_Files as `source-file` and `file` values.

### Requirement 4: Path-free client-facing error messages

**User Story:** As a security reviewer, I want tool error messages to omit resolved paths and storage locations, so that failures do not disclose deployment details either.

#### Acceptance Criteria

1. THE Server SHALL emit Client_Facing_Messages that contain no Storage_Location.
2. IF the OscalStore raises an error while a tool handles a call, THEN THE tool SHALL report a Client_Facing_Message that states the failed operation without the DB path, a Scan_Root path, or a `file_path` value.
3. IF a Bedrock Knowledge Base call fails, THEN THE Documentation_Search_Tool SHALL emit Client_Facing_Messages that contain no KB `location`, no `s3://` URI, and no AWS resource ARN.
4. IF `oscal-cli` output names the temporary file that `validate_oscal_content` wrote, THEN THE validator SHALL replace the temporary file path in the returned errors with a fixed placeholder that contains no Storage_Location.
5. IF a Doc_Fetch_Tool call names an unknown Doc_ID, THEN THE Doc_Fetch_Tool SHALL report an error that does not echo the rejected value.
6. WHEN the Server logs an error at any level, THE Server SHALL keep path details in the log record only, not in the Client_Facing_Message.

### Requirement 5: Path_Leak_Test over every registered tool

**User Story:** As a maintainer, I want a test that fails when any registered tool leaks a storage location, so that the rule stays enforced as tools are added.

#### Acceptance Criteria

1. THE Path_Leak_Test SHALL call every tool in the Tool_Registry at least once against the Fixture_Store, with arguments that produce a successful result for that tool.
2. THE Path_Leak_Test SHALL call each tool that accepts an identifying parameter at least once with an unknown value and check the resulting Client_Facing_Messages.
3. WHEN a tool result or Client_Facing_Message contains the Fixture_Store DB path, the Fixture_Store documents directory, the test temporary directory, the user's home directory, the installed package directory, or the substring `s3://`, THE Path_Leak_Test SHALL fail and name the tool.
4. THE Path_Leak_Test SHALL call the Documentation_Search_Tool in KB_Mode with a stubbed Bedrock client whose KB_Hits carry an `s3://` location and metadata, and SHALL fail if any of those values appear in the result.
5. THE Path_Leak_Test SHALL accept Metaschema_Source_Files in `source-file` and `file` values.
6. WHEN a tool is added to the Tool_Registry without arguments for the Path_Leak_Test, THE Path_Leak_Test SHALL fail and name the tool.

### Requirement 6: Resolvable text search hits

**User Story:** As an agent, I want every `text_search_oscal` hit to carry the identifiers that the fetch tools accept, so that I can read the full element after searching.

#### Acceptance Criteria

1. WHEN the Text_Search_Tool returns a hit for a Child_Element, THE Text_Search_Tool SHALL set `entity_id` to the Child_Element's Element_ID and SHALL include `parentDocumentUuid` set to the Document_UUID of the containing OSCAL_Document.
2. WHEN the Text_Search_Tool returns a hit for an OSCAL_Document, THE Text_Search_Tool SHALL set `entity_id` to the Document_UUID.
3. WHEN the Child_Fetch_Tool is called with the `entity_id` and `parentDocumentUuid` of any Child_Element hit, THE Child_Fetch_Tool SHALL return that Child_Element.
4. WHEN a Per_Model_Query_Tool for the hit's `model_type` is called with the `entity_id` of an OSCAL_Document hit as the document UUID, THE Per_Model_Query_Tool SHALL return that OSCAL_Document.
5. THE Text_Search_Tool SHALL keep the existing item keys `entity_type`, `entity_id`, `title`, `description`, and `model_type`.
6. THE Text_Search_Tool SHALL omit internal SQLite and FTS row ids from every item.

### Requirement 7: Text search scope, freshness, and size

**User Story:** As an agent, I want `text_search_oscal` to cover only OSCAL content, include newly loaded documents, and return bounded responses, so that results are relevant and fit my context.

#### Acceptance Criteria

1. THE Text_Search_Tool SHALL exclude Documentation_Documents of every Origin from results.
2. WHEN the Text_Search_Tool is called while any OSCAL_Document has `indexed=0`, THE OscalStore SHALL index those OSCAL_Documents before running the search.
3. IF indexing an OSCAL_Document fails during a Text_Search_Tool call, THEN THE OscalStore SHALL log a warning naming the Document_UUID and continue the search with the remaining documents.
4. WHEN a hit's description exceeds 500 characters, THE Text_Search_Tool SHALL shorten the description to 500 characters followed by TRUNCATION_MARK.
5. THE Text_Search_Tool SHALL return a Page_Response whose serialized JSON length is at most RESPONSE_BUDGET, holding fewer than `limit` items when needed and at least one item when any match exists.
6. WHERE `oscal_model_type` is given, THE Text_Search_Tool SHALL return only hits whose `model_type` equals that value.
7. THE Text_Search_Tool SHALL describe its `limit` parameter with the shared `BudgetedLimit` alias (default 10, range 1-100), keeping the parameter name and range unchanged.

### Requirement 8: Query normalization and relevance ranking

**User Story:** As an agent, I want hyphenated identifiers and natural-language questions to return relevance-ranked results, so that searches for `ac-2` or "how does profile resolution work" find the right content.

#### Acceptance Criteria

1. WHEN the Text_Search_Tool or the Documentation_Search_Tool receives query text, THE OscalStore SHALL apply Query_Normalization before any FTS5 `MATCH`.
2. WHEN the query text contains a hyphenated token such as `back-matter`, `ac-2`, or `control-implementation`, THE OscalStore SHALL match that token as one FTS5 phrase and return results without raising or catching `sqlite3.OperationalError`.
3. WHEN the query text contains FTS5 operator characters or keywords (`"`, `*`, `:`, `(`, `)`, `^`, `AND`, `OR`, `NOT`, `NEAR`), THE OscalStore SHALL treat them as literal text or separators.
4. WHEN the query text contains Stop_Words and at least one other word, THE OscalStore SHALL drop the Stop_Words from the Search_Terms.
5. THE OscalStore SHALL return every entity that matches at least one Search_Term.
6. THE OscalStore SHALL order entities that match every Search_Term before entities that match only some Search_Terms, and SHALL order entities within each of those two groups by FTS5 rank.
7. WHEN the bundled documentation is searched for "how does profile resolution work", THE Documentation_Search_Tool SHALL return every Documentation_Document that a search for "profile resolution" returns.
8. IF Query_Normalization yields no Search_Terms, THEN THE OscalStore SHALL return a Page_Response with an empty `items` list and `total` 0.
9. IF an FTS5 `MATCH` on normalized Search_Terms raises `sqlite3.OperationalError`, THEN THE OscalStore SHALL log a warning and fall back to a case-insensitive `LIKE` search that returns the same item shape.

### Requirement 9: Searchable catalog control prose

**User Story:** As an agent, I want catalog control statements and guidance indexed, so that content searches find the controls that discuss a topic.

#### Acceptance Criteria

1. WHEN the OscalStore extracts a catalog control Child_Element, THE OscalStore SHALL set the Child_Element description to the control's Control_Prose.
2. THE OscalStore SHALL index the Control_Prose in the `fts_index` description of the control's row.
3. WHEN a control has no `statement` or `guidance` part, THE OscalStore SHALL set the description to an empty string.
4. THE OscalStore SHALL apply criteria 1-3 to controls at every nesting level that the OscalStore extracts, including controls inside nested groups.
5. WHEN `hatch run build-db` completes, THE bundled DB SHALL contain catalog control rows whose description holds Control_Prose for every bundled control that has a `statement` or `guidance` part.
6. WHEN the Text_Search_Tool searches for a phrase that occurs only in a fixture control's guidance, THE Text_Search_Tool SHALL return that control as a hit.

### Requirement 10: Documentation search paging and result shape

**User Story:** As an agent, I want documentation search to return one paged shape in every mode, so that I can handle results the same way with or without a Knowledge Base.

#### Acceptance Criteria

1. THE Documentation_Search_Tool SHALL accept `offset` (the shared `Offset` alias, default 0) and `limit` (the shared `BudgetedLimit` alias, default 10, range 1-100) parameters.
2. THE Documentation_Search_Tool SHALL return a Page_Response whose `items` are Doc_Result_Items in both KB_Mode and Local_Mode.
3. WHEN the Documentation_Search_Tool returns a Local_Hit, THE Documentation_Search_Tool SHALL set `docId` to the Local_Hit's Doc_ID, `title` to the document title, and `snippet` to at most 200 characters of matched content.
4. WHEN the Documentation_Search_Tool returns a KB_Hit, THE Documentation_Search_Tool SHALL set `snippet` to the KB_Hit passage text and `title` to a title derived from the KB_Hit that contains no Storage_Location, and SHALL omit `docId`.
5. IF no title can be derived for a KB_Hit, THEN THE Documentation_Search_Tool SHALL set `title` to an empty string.
6. IF `offset` is negative or `limit` is outside 1-100, THEN THE Documentation_Search_Tool SHALL report a Client_Facing_Message that names the parameter and its valid range.
7. WHEN the query text is empty or whitespace-only, THE Documentation_Search_Tool SHALL return a Page_Response with an empty `items` list and `total` 0.

### Requirement 11: User documentation in KB_Mode

**User Story:** As a user with a Knowledge Base configured, I want my own documentation from `OSCAL_DOCUMENTS_DIR` included in documentation search, so that configuring a KB does not hide my documents.

#### Acceptance Criteria

1. WHILE in KB_Mode, THE Documentation_Search_Tool SHALL search the Knowledge Base and the Documentation_Documents with Origin `user`, and SHALL merge both result lists into one sequence.
2. WHILE in KB_Mode, THE Documentation_Search_Tool SHALL exclude Documentation_Documents with Origin `bundled` from the merged sequence.
3. THE Documentation_Search_Tool SHALL preserve the Knowledge Base order among KB_Hits and the Requirement 8 order among Local_Hits in the merged sequence.
4. THE Documentation_Search_Tool SHALL interleave the merged sequence one KB_Hit then one Local_Hit, and SHALL append the remaining hits of the longer list after the shorter list is exhausted.
5. WHILE in KB_Mode, THE Documentation_Search_Tool SHALL return the slice of the merged sequence from position `offset` with at most `limit` items, and SHALL request at most 100 KB_Hits from the Knowledge Base per call.
6. WHEN consecutive pages are requested with the same query and unchanged Knowledge Base results, THE Documentation_Search_Tool SHALL return no item twice and skip no item of the merged sequence.
7. WHILE in Local_Mode, THE Documentation_Search_Tool SHALL search Documentation_Documents of both Origins.
8. IF the Knowledge Base call fails, THEN THE Documentation_Search_Tool SHALL log a warning and return Local_Mode results over Documentation_Documents of both Origins.
9. WHEN a Documentation_Document from `OSCAL_DOCUMENTS_DIR` matches the query, THE Documentation_Search_Tool SHALL return that document as a Local_Hit in both KB_Mode and Local_Mode.

### Requirement 12: Documentation fetch tool

**User Story:** As an agent, I want to read the full text of a documentation hit by its ID, so that I can go from a snippet to the source content without a filesystem path.

#### Acceptance Criteria

1. THE Tool_Registry SHALL include the Doc_Fetch_Tool `get_oscal_documentation`.
2. THE Doc_Fetch_Tool SHALL accept a required `doc_id` parameter, a `start_line` parameter (1-based, default 1), and a `line_count` parameter (range 1-500, default 200).
3. WHEN the Doc_Fetch_Tool is called with a Doc_ID of a Documentation_Document of either Origin, THE Doc_Fetch_Tool SHALL return `docId`, `title`, `start-line`, `end-line`, `total-lines`, `has-more`, and `text`, where `text` is the verbatim document lines from `start-line` through `end-line`.
4. THE Doc_Fetch_Tool SHALL look up `doc_id` by exact match against stored Doc_IDs before reading any document text.
5. IF `doc_id` does not equal a stored Doc_ID, THEN THE Doc_Fetch_Tool SHALL report a Client_Facing_Message that states the ID is unknown and names `query_oscal_documentation` as the source of Doc_IDs, without echoing the rejected value.
6. IF `doc_id` equals the Document_UUID of an OSCAL_Document, THEN THE Doc_Fetch_Tool SHALL report the same unknown-ID error as criterion 5.
7. IF `start_line` is outside 1 to the document's line count or `line_count` is outside 1-500, THEN THE Doc_Fetch_Tool SHALL report a Client_Facing_Message that names the parameter and its valid range.
8. THE Doc_Fetch_Tool SHALL return a response whose serialized JSON length is at most RESPONSE_BUDGET, holding fewer than `line_count` lines when needed and at least one line.
9. IF one line alone exceeds RESPONSE_BUDGET, THEN THE Doc_Fetch_Tool SHALL shorten that line at a code-point boundary, append TRUNCATION_MARK, and set `truncated` to true.
10. WHEN a caller pages from `start_line` 1 by setting each next `start_line` to the previous `end-line` + 1 while `has-more` is true, THE Doc_Fetch_Tool SHALL return every line of the document exactly once.

### Requirement 13: Tool authoring, budget, and scope boundaries

**User Story:** As a maintainer, I want the new and changed tools to follow the project's authoring rules and stay within the context budget, so that clients pay no unexpected context cost.

#### Acceptance Criteria

1. THE Doc_Fetch_Tool, the changed Documentation_Search_Tool, and the changed Text_Search_Tool SHALL follow `.kiro/steering/tool-authoring.md`: Description_Shape, `ParamDoc` parameter descriptions, shared aliases for shared parameters, and `ctx: Context | None = None` last.
2. THE Server SHALL keep the compact JSON of the `tools/list` `tools` array at or below TOTAL_BUDGET, and every tool and parameter description within its cap in `tool_budget.py`.
3. THE Server SHALL keep every existing tool name and every existing tool parameter.
4. THE Server SHALL keep `"documentation"` out of `OSCALModelType`, `MODEL_MAP`, and every model-name parameter's accepted values.
5. THE Routing_Guide SHALL name `get_oscal_documentation` as the fetch step after `query_oscal_documentation`, and `get_child_element` as the fetch step after `text_search_oscal`, within the `ROUTING_GUIDE_CAP`.
6. WHEN this feature is complete, THE Sync_Targets, `README.md`, and the `AGENTS.md` code map SHALL list `get_oscal_documentation` and describe the changed result shapes.
7. WHEN this feature is complete, `hatch run tests` SHALL pass.

### Requirement 14: Bundled DB rebuild and existing DB upgrade

**User Story:** As a maintainer, I want the bundled DB rebuilt and older persistent DBs upgraded, so that Origin, Doc_IDs, and catalog prose are present in every DB mode.

#### Acceptance Criteria

1. WHEN `hatch run build-db` runs, THE build script SHALL produce a bundled DB in which every `documents` row has Origin `bundled`, every Documentation_Document has a Doc_ID per Requirement 2, and catalog controls carry Control_Prose per Requirement 9.
2. WHEN the OscalStore opens a DB that predates this feature, THE OscalStore SHALL upgrade the DB schema in place so that every `documents` row has an Origin.
3. WHEN the OscalStore upgrades a pre-feature DB, THE OscalStore SHALL re-extract the Child_Elements of every catalog OSCAL_Document before the next search or child lookup on that document.
4. WHEN the Server starts with a persistent DB that predates this feature and `OSCAL_DOCUMENTS_DIR` is set, THE Server SHALL record Origin `user` and Requirement 2 Doc_IDs for every document scanned from `OSCAL_DOCUMENTS_DIR`.
5. THE `tests/test_build_oscal_db.py` tests SHALL check criterion 1 against a build from fixture directories.

### Requirement 15: Follow-up issue for full-text fetch of KB hits

**User Story:** As the project owner, I want full-text fetch of Knowledge Base hits tracked as its own issue, so that the deferred work is not lost.

#### Acceptance Criteria

1. THE maintainer SHALL create the GitHub issue for full-text fetch of KB_Hits as an explicit task early in implementation, before the Documentation_Search_Tool changes are implemented.
2. THE follow-up issue SHALL reference #45 and record the open question from #45: whether KB_Hits can map back to bundled Documentation_Documents (for example, by mapping the KB data source S3 object key to a bundled Relative_Name).
3. WHEN the follow-up issue exists, THE maintainer SHALL link the follow-up issue from #45 with a comment.
4. THE Doc_Result_Item shape SHALL allow a future KB_Hit Doc_ID (for example, one derived from Origin `kb` or from a mapped bundled Relative_Name) to be added through the existing `docId` key, with no other change to the Doc_Result_Item shape.

## Out of Scope

- Full-text fetch of KB_Hits (KB_Hits carry no Doc_ID and cannot be passed to the Doc_Fetch_Tool). Tracked by the follow-up issue in Requirement 15; the design must not foreclose it (Requirement 15.4).
- Renaming, removing, or consolidating tools (#40).
- Changing what `validate_oscal_file` may read; only its Client_Facing_Messages are covered by Requirement 4.
- Indexing prose of catalog groups, profiles, or other model types beyond what the OscalStore already indexes.
