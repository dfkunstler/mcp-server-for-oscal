#!/bin/bash

#==============================================================================
# OSCAL Schema Update Script
#==============================================================================
# This script downloads and updates OSCAL (Open Security Controls Assessment 
# Language) schemas from the official NIST OSCAL GitHub repository.
#
# Purpose:
#   - Downloads the latest OSCAL schema files (JSON and XSD formats)
#   - Updates the local schema directory used by the MCP server
#   - Ensures the server has access to current OSCAL model definitions
#
# Usage: ./bin/update-oscal-schemas.sh
#
# The script will:
#   1. Download the OSCAL release package (CURRENT_RELEASE_VERSION) from GitHub
#      and verify it against CURRENT_RELEASE_SHA256; a mismatch stops the run
#      before anything in the repo changes
#   2. Update the bundled Metaschema sources (oscal_metaschema/) from the
#      commit archive of CURRENT_RELEASE_COMMIT via update_oscal_metaschema.py
#   3. Extract JSON schema files and XSD files into a staging directory
#   4. Copy them into the project's schema directory
#   5. Clean up temporary files
#==============================================================================

# Determine script directory using POSIX-compliant method
# This ensures the script works regardless of where it's called from
SCRIPT_DIR="$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CURRENT_RELEASE_VERSION="1.2.3"
# Commit that tag v$CURRENT_RELEASE_VERSION points to (dereference annotated tags):
#   gh api repos/usnistgov/OSCAL/git/ref/tags/v$CURRENT_RELEASE_VERSION
#   gh api repos/usnistgov/OSCAL/git/tags/<tag-object-sha> --jq .object.sha
CURRENT_RELEASE_COMMIT="e061961c7702afeabffdf5e59894d35e748dade1"

# SHA-256 of the release asset oscal-$CURRENT_RELEASE_VERSION.zip. Update it together
# with CURRENT_RELEASE_VERSION and compare it with the digest GitHub reports:
#   gh api repos/usnistgov/OSCAL/releases/tags/v$CURRENT_RELEASE_VERSION \
#     --jq '.assets[] | select(.name == "oscal-'$CURRENT_RELEASE_VERSION'.zip") | .digest'
CURRENT_RELEASE_SHA256="eefb4ec5b1383d3b15fca27e0b63bc84b8661d6ee17e4f92acb437606e9ee982"
# Define the target directory where OSCAL schemas will be stored
# This is where the MCP server will look for schema definitions
SCHEMAS_DIR="$PROJECT_ROOT/src/mcp_server_for_oscal/oscal_schemas"
# OSCAL release URL - points to official NIST OSCAL release
# Contains JSON schemas, XSD files, and other OSCAL model definitions
OSCAL_RELEASE_URL="https://github.com/usnistgov/OSCAL/releases/download/v$CURRENT_RELEASE_VERSION/oscal-$CURRENT_RELEASE_VERSION.zip"
# Work in a private temporary directory that is removed on exit, so a failed
# run leaves no partial files behind
WORK_DIR="$(mktemp -d)" || exit 1
trap 'rm -rf "$WORK_DIR"' EXIT
OSCAL_RELEASE_FILE="$WORK_DIR/oscal-$CURRENT_RELEASE_VERSION.zip"
STAGING_DIR="$WORK_DIR/schemas"
mkdir "$STAGING_DIR" || exit 1
# Download the OSCAL release archive over HTTPS only.
# --fail makes HTTP errors fatal, -L follows redirects (GitHub serves assets from a CDN)
echo "Downloading OSCAL schemas from: $OSCAL_RELEASE_URL"
curl --fail --silent --show-error --proto '=https' --proto-redir '=https' -L \
    -o "$OSCAL_RELEASE_FILE" "$OSCAL_RELEASE_URL" || exit 1
# Verify the archive against the pinned digest before using any of it
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(sha256sum "$OSCAL_RELEASE_FILE" | cut -d ' ' -f 1)"
else
    ACTUAL_SHA256="$(shasum -a 256 "$OSCAL_RELEASE_FILE" | cut -d ' ' -f 1)"
fi
if [ "$ACTUAL_SHA256" != "$CURRENT_RELEASE_SHA256" ]; then
    echo "ERROR: SHA-256 mismatch for $OSCAL_RELEASE_URL" >&2
    echo "  expected: $CURRENT_RELEASE_SHA256" >&2
    echo "  actual:   $ACTUAL_SHA256" >&2
    echo "Nothing was changed. If the release was legitimately updated, review it and" >&2
    echo "update CURRENT_RELEASE_SHA256 in $0." >&2
    exit 1
fi
# Update the bundled Metaschema sources from the commit archive of
# CURRENT_RELEASE_COMMIT. A failure here stops before the JSON/XSD schemas
# change, so the two bundles never move to different versions in one run.
# Runs under `hatch run update-oscal-schemas`, so `python` is the hatch env interpreter.
python "$SCRIPT_DIR/update_oscal_metaschema.py" --version "$CURRENT_RELEASE_VERSION" \
    --commit "$CURRENT_RELEASE_COMMIT" \
    --dest "$PROJECT_ROOT/src/mcp_server_for_oscal/oscal_metaschema" || exit 1
# Extract only JSON and XSD files from the archive into the staging directory
# -j flattens directory structure (junk paths), -o overwrites existing files
echo "Extracting schema files"
unzip -j -o "$OSCAL_RELEASE_FILE" "*.json" "*.xsd" -d "$STAGING_DIR" || exit 1
# Remove all white space from json files
for json_file in "$STAGING_DIR"/*.json; do
    jq -c . "$json_file" > "$WORK_DIR/tmpws.json" || exit 1
    mv "$WORK_DIR/tmpws.json" "$json_file"
done
# Copy the finished files into the schema directory
echo "Copying schema files to: $SCHEMAS_DIR"
cp -f "$STAGING_DIR"/* "$SCHEMAS_DIR"/ || exit 1
echo "OSCAL schema update completed successfully!"
