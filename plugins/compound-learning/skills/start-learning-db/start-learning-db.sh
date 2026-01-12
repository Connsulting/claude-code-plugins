#!/usr/bin/env bash
# Start ChromaDB container for learning system

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting ChromaDB container for learning system..."

# Call the start-chromadb.sh script in the same directory
"${SCRIPT_DIR}/start-chromadb.sh"

echo ""
echo "Learning database is ready!"
echo ""
echo "Next steps:"
echo "1. Run /compound after sessions to build your learning database"
echo "2. Learnings will be automatically searched using /search-learnings skill"
