#!/usr/bin/env bash
# Start ChromaDB using docker run (replaces docker-compose)

set -e

# Configuration priority: environment variables > config file > defaults
DEFAULT_PORT=8000
DEFAULT_DATA_DIR="${HOME}/.claude/chroma-data"

# Check environment variables first (highest priority)
ENV_PORT="${CHROMADB_PORT:-}"
ENV_DATA_DIR="${CHROMADB_DATA_DIR:-}"

# Read config file if it exists
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${HOME}/.claude}"
CONFIG_FILE="${PLUGIN_ROOT}/.claude-plugin/config.json"
CONFIG_PORT=""
CONFIG_DATA_DIR=""

if [ -f "$CONFIG_FILE" ]; then
  if command -v jq &> /dev/null; then
    CONFIG_PORT=$(jq -r '.chromadb.port // empty' "$CONFIG_FILE")
    CONFIG_DATA_DIR=$(jq -r '.chromadb.dataDir // empty' "$CONFIG_FILE")
  else
    CONFIG_PORT=$(grep -o '"port"[[:space:]]*:[[:space:]]*[0-9]*' "$CONFIG_FILE" | grep -o '[0-9]*' | head -1)
    CONFIG_DATA_DIR=$(grep -o '"dataDir"[[:space:]]*:[[:space:]]*"[^"]*"' "$CONFIG_FILE" | sed 's/.*"\([^"]*\)".*/\1/')
  fi
fi

# Apply priority: env > config > default
CHROMA_PORT="${ENV_PORT:-${CONFIG_PORT:-$DEFAULT_PORT}}"
CHROMA_DATA_DIR="${ENV_DATA_DIR:-${CONFIG_DATA_DIR:-$DEFAULT_DATA_DIR}}"

# Expand ${HOME} and ~ in data dir
CHROMA_DATA_DIR="${CHROMA_DATA_DIR/\$\{HOME\}/$HOME}"
CHROMA_DATA_DIR="${CHROMA_DATA_DIR/#\~/$HOME}"

CONTAINER_NAME="claude-learning-db"

echo "Starting ChromaDB container..."
echo "  Port: $CHROMA_PORT"
echo "  Data directory: $CHROMA_DATA_DIR"

# Create data directory if it doesn't exist
mkdir -p "$CHROMA_DATA_DIR"

# Check if container already exists
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "Container $CONTAINER_NAME already exists"

  # Check if it's running
  if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "[OK] Container is already running"
  else
    echo "Starting existing container..."
    docker start "$CONTAINER_NAME"
    echo "[OK] Container started"
  fi
else
  echo "Creating new ChromaDB container..."
  docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${CHROMA_PORT}:8000" \
    -v "${CHROMA_DATA_DIR}:/data" \
    -e IS_PERSISTENT=TRUE \
    -e PERSIST_DIRECTORY=/data \
    -e ANONYMIZED_TELEMETRY=FALSE \
    -e ALLOW_RESET=TRUE \
    --restart unless-stopped \
    chromadb/chroma:latest

  echo "[OK] Container created and started"
fi

# Wait for ChromaDB to be ready
echo "Waiting for ChromaDB to be ready..."
sleep 5

# Check if container is running
if docker ps | grep -q "$CONTAINER_NAME"; then
  echo "[OK] ChromaDB container is running"

  # Try to connect
  if curl -s "http://localhost:${CHROMA_PORT}/" > /dev/null 2>&1; then
    echo "[OK] ChromaDB is accessible on http://localhost:${CHROMA_PORT}"
  else
    echo "[WARN] Container running but port may still be initializing"
    echo "  Give it a few more seconds or check: docker logs $CONTAINER_NAME"
  fi
else
  echo "[ERROR] ChromaDB container failed to start. Check docker logs:"
  echo "  docker logs $CONTAINER_NAME"
  exit 1
fi

echo ""
echo "ChromaDB is ready!"
