# Bugfix Requirements Document

## Introduction

`get_oscal_schema(schema_type="xsd")` always fails (GitHub issue #13). The tool parses every schema file as JSON, so requesting an XSD schema raises a JSON decode error and no client can retrieve any OSCAL XML schema, even though the XSD files are bundled for all eight models plus `complete`. The existing XSD unit test mocks the JSON parser, which hides the defect. A secondary defect is that the schema file handle opened by the tool is never closed, for both JSON and XSD requests.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `get_oscal_schema` is called with `schema_type="xsd"` and any valid `model_name` (including `complete`) THEN the system raises a JSON decode error instead of returning the schema

1.2 WHEN `get_oscal_schema` is called with `schema_type="xsd"` THEN the system reports a generic "failed to open schema" error to the MCP client, even though the bundled XSD file exists and is readable

1.3 WHEN `get_oscal_schema` is called with any valid `schema_type` and `model_name` THEN the system leaves the schema file handle open after the call returns or raises

1.4 WHEN the tool description is read by an MCP client or LLM THEN the system documents the return value only as "The requested schema as JSON string", which is wrong for XSD requests

### Expected Behavior (Correct)

2.1 WHEN `get_oscal_schema` is called with `schema_type="xsd"` and any valid `model_name` (including `complete`) THEN the system SHALL return the raw XSD text of the matching bundled schema file, identical to the file's contents

2.2 WHEN `get_oscal_schema` is called with `schema_type="xsd"` and the bundled XSD file cannot be opened or read THEN the system SHALL report "failed to open schema <file name>" to the client and re-raise the underlying error

2.3 WHEN `get_oscal_schema` is called with any valid `schema_type` and `model_name` THEN the system SHALL close the schema file handle before returning or raising

2.4 WHEN the tool description is read by an MCP client or LLM THEN the system SHALL document that JSON requests return the schema as a JSON string and XSD requests return the schema as XML text

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `get_oscal_schema` is called with `schema_type="json"` (or the default) and any valid `model_name` (including `complete`) THEN the system SHALL CONTINUE TO return a JSON string that parses to the same object as the bundled JSON schema file

3.2 WHEN `get_oscal_schema` is called with no arguments THEN the system SHALL CONTINUE TO return the `complete` JSON schema

3.3 WHEN `get_oscal_schema` is called with a `schema_type` other than `json` or `xsd` THEN the system SHALL CONTINUE TO notify the client with "Invalid schema type: <value>." and raise `ValueError`

3.4 WHEN `get_oscal_schema` is called with a `model_name` that is not a valid OSCAL model type or `complete` THEN the system SHALL CONTINUE TO notify the client with the existing "Invalid model" message and raise `ValueError`

3.5 WHEN a JSON schema file cannot be opened or contains invalid JSON THEN the system SHALL CONTINUE TO notify the client with "failed to open schema <file name>" and re-raise the underlying error

3.6 WHEN `get_oscal_schema` resolves a schema file name THEN the system SHALL CONTINUE TO use the `schema_names` mapping and read files only from the bundled `oscal_schemas` directory
