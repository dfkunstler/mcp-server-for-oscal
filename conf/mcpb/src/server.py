#!/usr/bin/env python3
"""MCPB entry point for the OSCAL MCP server."""

import os
import re

# Environment variables set from user_config in manifest.json. MCPB hosts pass
# unset optional values through as empty strings or as the literal
# "${user_config.<key>}" placeholder. Drop those so the server falls back to
# its own defaults (e.g. an empty AWS_PROFILE breaks boto3).
USER_CONFIG_ENV = (
    "OSCAL_DOCUMENTS_DIR",
    "OSCAL_ALLOW_REMOTE_URIS",
    "OSCAL_KB_ID",
    "AWS_PROFILE",
    "AWS_REGION",
    "LOG_LEVEL",
)
_UNSET = re.compile(r"^\$\{user_config\.[^}]+\}$")
for _key in USER_CONFIG_ENV:
    _value = os.environ.get(_key)
    if _value is not None and (_value.strip() == "" or _UNSET.match(_value)):
        del os.environ[_key]

from mcp_server_for_oscal.main import main  # noqa: E402

if __name__ == "__main__":
    main()
