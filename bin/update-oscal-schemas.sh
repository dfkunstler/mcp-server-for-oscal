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
#   1. Update the bundled Metaschema sources (oscal_metaschema/) from the
#      commit archive of CURRENT_RELEASE_COMMIT via update_oscal_metaschema.py
#   2. Download the OSCAL release package (CURRENT_RELEASE_VERSION) from GitHub
#   3. Extract JSON schema files and XSD files 
#   4. Place them in the project's schema directory
#   5. Clean up temporary download files
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

# Update the bundled Metaschema sources first, from the commit archive of
# CURRENT_RELEASE_COMMIT. A failure here stops before the JSON/XSD schemas
# change, so the two bundles never move to different versions in one run.
# Runs under `hatch run update-oscal-schemas`, so `python` is the hatch env interpreter.
python "$SCRIPT_DIR/update_oscal_metaschema.py" --version "$CURRENT_RELEASE_VERSION" \
    --commit "$CURRENT_RELEASE_COMMIT" \
    --dest "$PROJECT_ROOT/src/mcp_server_for_oscal/oscal_metaschema" || exit 1

# Define the target directory where OSCAL schemas will be stored
# This is where the MCP server will look for schema definitions
SCHEMAS_DIR="$PROJECT_ROOT/src/mcp_server_for_oscal/oscal_schemas"

# OSCAL release URL - points to official NIST OSCAL release
# Contains JSON schemas, XSD files, and other OSCAL model definitions
OSCAL_RELEASE_URL="https://github.com/usnistgov/OSCAL/releases/download/v$CURRENT_RELEASE_VERSION/oscal-$CURRENT_RELEASE_VERSION.zip"

# Determine temporary download directory
# Uses system temp directory (TMPDIR, TMP, TEMP, or defaults to /tmp)
DOWNLOAD_DIR=${TMPDIR:-${TMP:-${TEMP:-/tmp}}}

# Extract filename from the URL for local storage
OSCAL_RELEASE_FILE_NAME=$(basename "$OSCAL_RELEASE_URL")

# Download the OSCAL release archive
# -L flag follows redirects, -o specifies output file
echo "Downloading OSCAL schemas from: $OSCAL_RELEASE_URL"
curl -L -o "$DOWNLOAD_DIR"/"$OSCAL_RELEASE_FILE_NAME" $OSCAL_RELEASE_URL

# Extract only JSON and XSD files from the archive
# -d specifies destination directory
# -j flattens directory structure (junk paths)
# -o overwrites existing files
# "*.json" "*.xsd" filters to only extract schema files
echo "Extracting schema files to: $SCHEMAS_DIR"
unzip -j -o "${DOWNLOAD_DIR}/${OSCAL_RELEASE_FILE_NAME}" "*.json" "*.xsd" -d "$SCHEMAS_DIR" 

# Remove all white space from json files
for json_file in "$SCHEMAS_DIR"/*.json; do
    jq -c . "$json_file" > "$SCHEMAS_DIR"/tmpws.json && mv "$SCHEMAS_DIR"/tmpws.json "$json_file"
done

# Clean up: remove the downloaded archive file
echo "Cleaning up temporary files..."
rm -f "$SCHEMAS_DIR"/tmpws.json
rm -f "${DOWNLOAD_DIR}/${OSCAL_RELEASE_FILE_NAME}"

echo "OSCAL schema update completed successfully!"
