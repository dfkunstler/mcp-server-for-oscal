# Bugfix Requirements Document

## Introduction

GitHub issue #14: `query_oscal_documentation` calls Amazon Bedrock even when no Knowledge Base is configured.

The tool decides between the Bedrock Knowledge Base path and the local documentation search path by checking whether the configured KB ID is "not None". The configured KB ID defaults to an empty string when `OSCAL_KB_ID` is unset, and the CLI override only applies non-empty values, so the KB ID is never None at runtime. Every documentation query from a user who never configured AWS therefore attempts a Bedrock call with an empty KB ID. The call fails (or stalls while resolving AWS credentials), produces warning and exception logs, sends a misleading error notification to the MCP client, and only then falls back to local search. Users see extra latency, misleading errors, and an unexpected outbound AWS network call.

The existing routing tests pass because they simulate the unset case with a None KB ID, a value that cannot occur at runtime.

Decision on whitespace-only values: a KB ID consisting only of whitespace (spaces, tabs, newlines) is treated as unset. Such a value can only come from a configuration mistake (for example `OSCAL_KB_ID=" "` in a `.env` file) and can never identify a real Knowledge Base, so routing it to Bedrock would reproduce the same defect.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `OSCAL_KB_ID` is unset and no `--knowledge-base-id` CLI argument is given (KB ID is `""`) AND `query_oscal_documentation` is called THEN the system routes the query to the Bedrock Knowledge Base path instead of the local search path

1.2 WHEN the KB ID is `""` AND `query_oscal_documentation` is called THEN the system creates an AWS session and issues a Bedrock `retrieve` request with an empty KB ID, making an outbound network call (or blocking on credential resolution) for a user who never configured AWS

1.3 WHEN the KB ID is `""` AND `query_oscal_documentation` is called THEN the system logs that it is using the Knowledge Base search path, logs an exception for the failed Bedrock call, and logs a warning that it is falling back to local search

1.4 WHEN the KB ID is `""` AND `query_oscal_documentation` is called with an MCP context THEN the system sends an error notification to the MCP client describing a failed documentation query, even though the user receives local search results

1.5 WHEN the KB ID is whitespace-only (e.g. `" "`) AND `query_oscal_documentation` is called THEN the system routes the query to the Bedrock Knowledge Base path with the same consequences as 1.2–1.4

### Expected Behavior (Correct)

2.1 WHEN the KB ID is `""` AND `query_oscal_documentation` is called THEN the system SHALL route the query directly to the local documentation search path and return its results

2.2 WHEN the KB ID is `""` AND `query_oscal_documentation` is called THEN the system SHALL NOT create an AWS session or issue any Bedrock request

2.3 WHEN the KB ID is `""` AND `query_oscal_documentation` is called THEN the system SHALL log that it is using the local documentation search path and SHALL NOT log a Knowledge Base path message, a Bedrock exception, or a fallback warning

2.4 WHEN the KB ID is `""` AND `query_oscal_documentation` is called with an MCP context THEN the system SHALL NOT send an error notification to the MCP client

2.5 WHEN the KB ID is whitespace-only AND `query_oscal_documentation` is called THEN the system SHALL treat the KB ID as unset and behave as in 2.1–2.4

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the KB ID contains at least one non-whitespace character (set via `OSCAL_KB_ID` or `--knowledge-base-id`) AND `query_oscal_documentation` is called THEN the system SHALL CONTINUE TO route the query to the Bedrock Knowledge Base path, passing the configured KB ID unchanged, and return the Bedrock response

3.2 WHEN the KB ID contains at least one non-whitespace character AND the Bedrock query raises an error THEN the system SHALL CONTINUE TO log the failure, notify the MCP client of the error, log a fallback warning, and return local documentation search results

3.3 WHEN the KB ID contains at least one non-whitespace character AND `query_oscal_documentation` is called THEN the system SHALL CONTINUE TO log that it is using the Knowledge Base search path, including the KB ID

3.4 WHEN a configured AWS profile is set AND the Knowledge Base path is taken THEN the system SHALL CONTINUE TO create the AWS session with that profile

3.5 WHEN the local documentation search path is taken AND the document store has not been initialised THEN the system SHALL CONTINUE TO return an error dict stating that the store is not initialised

3.6 WHEN the tool list is requested THEN the system SHALL CONTINUE TO include `query_oscal_documentation` unconditionally, regardless of whether a KB ID is configured

3.7 WHEN a non-empty `--knowledge-base-id` CLI argument is given THEN the system SHALL CONTINUE TO override the `OSCAL_KB_ID` environment value, and WHEN the CLI argument is absent or empty THEN the system SHALL CONTINUE TO keep the environment value
