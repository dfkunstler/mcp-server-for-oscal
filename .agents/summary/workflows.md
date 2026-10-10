# Workflows

<!-- tags: workflows, build, release, ci, content-update, request-flow -->

## Tool request through the store

```mermaid
sequenceDiagram
    participant C as MCP client
    participant T as query_catalog (tool)
    participant S as OscalStore
    participant DB as SQLite (thread-local conn)
    participant P as oscal-bindings (LRU cached)
    C->>T: query_type=by_title, query_value="NIST 800-53"
    T->>S: query(model_type=catalog, ...)
    S->>DB: SELECT documents (NOCASE title; FTS fallback)
    S->>S: _ensure_indexed(doc_id) if indexed=0
    S->>P: parse raw_json (cached by doc_id + raw_json)
    S->>DB: INSERT child_elements + fts_index, set indexed=1
    S-->>T: page response
    T-->>C: JSON result
```

`query_component_definition` adds a scope step first: the filter must match a cdef UUID exactly, or its title case-insensitively. Fuzzy matches are rejected. Capabilities are matched before components, and only the candidate parent cdefs are parsed.

## Documentation query

```mermaid
flowchart TD
    Q[query_oscal_documentation] --> C{config.knowledge_base_id is not None}
    C -- true, including '' --> KB[query_kb: boto3 bedrock-agent-runtime.retrieve]
    KB -- exception --> L[query_local: store.search_documentation]
    KB -- ok --> R1[Bedrock RetrieveResponse]
    C -- false --> L
    L --> R2[page response of documentation hits, snippet up to 200 chars]
```

Because the default `OSCAL_KB_ID` is `""`, the KB path always runs first and fails (logged as a warning) before falling back. See review_notes.

## Local development loop

The hatch steering file requires all Python to run through hatch.

```mermaid
flowchart LR
    Edit[edit src/tests] --> T1[hatch test path::Test -- -x]
    T1 --> Lint[hatch check fmt --fix<br/>hatch check code --fix]
    Lint --> Full[hatch run tests<br/>mypy + pytest matrix 3.13/3.14 + coverage + bandit]
    Full --> Commit[commit on feature branch, reference #issue]
```

- `hatch run tests` uses `--exitfirst --all --cover` and writes `private/docs/pytest.xml`.
- pytest flags go after `--`, because `-x`, `-p`, `-c`, and `-r` mean something else to `hatch test`.
- Dependencies: edit `pyproject.toml`, then `hatch run update` re-locks `requirements.txt` (universal, py3.13) and syncs.

## Build and release

```mermaid
sequenceDiagram
    participant Dev
    participant CI as build.yml (push main / PR / tag v*)
    participant Rel as release.yml (release published)
    Dev->>CI: push / PR
    CI->>CI: mcp-publisher validate (server.json)
    CI->>CI: build job (Linux, Python 3.14): hatch run release
    Note over CI: tests -> git status -> refresh-nist-docs.sh -> build-db -> hatch build -> build-mcpb
    CI->>CI: upload private/docs, wheel+sdist, .mcpb artifacts
    CI->>CI: test job: hatch test on Linux, macOS, Windows x 3.13/3.14
    CI->>CI: mcpb job (macOS, Windows): download .mcpb, validate/pack/unpack, uv sync, stdio smoke test
    Dev->>CI: push tag vX.Y.Z (on main)
    CI->>CI: draft-release (needs build, test, mcpb): verify tag on main, gh release create --draft with artifacts
    Dev->>Rel: publish the draft release (manual)
    Rel->>Rel: download wheel/sdist from release -> PyPI (trusted publishing)
    Rel->>Rel: rewrite server.json versions from tag -> mcp-publisher publish (continue-on-error)
```

Every CI build refreshes OSCAL-Pages docs from upstream `main` and rebuilds the DB, so the bundled documentation can change without a code change.

## Updating bundled content

```mermaid
flowchart TD
    subgraph Schemas
        A1[edit CURRENT_RELEASE_VERSION in bin/update-oscal-schemas.sh] --> A2[hatch run update-oscal-schemas]
        A2 --> A3[hatch run rehash]
        A3 --> A4[tests: TestBundledOscalVersion checks script version == schema $id]
    end
    subgraph AWS cdefs
        B1[edit CURRENT_RELEASE_VERSION in bin/update-aws-cdefs.sh] --> B2[run script: zip to data/component_definitions]
        B2 --> B3[hatch run rehash]
        B3 --> B4[hatch run build-db]
    end
    subgraph awesome-oscal
        C1[nightly update-awesome-oscal.yml] --> C2[hatch run update-awesome-oscal -> data/oscal_docs/awesome-oscal.md]
        C2 --> C3[PR if changed]
    end
```

The README OSCAL badge regex-reads `CURRENT_RELEASE_VERSION` from `bin/update-oscal-schemas.sh` on `main`.

## Project process (from `.kiro/steering/git-strategy.md`)

GitHub issue → feature branch → commits that reference `#<issue>` and say whether they were tested → run `hatch run tests` before committing → push and open a PR only with user approval. Never commit to `main` without approval. Spec-driven features live in `.kiro/specs/<feature>/` (requirements, design, tasks).
