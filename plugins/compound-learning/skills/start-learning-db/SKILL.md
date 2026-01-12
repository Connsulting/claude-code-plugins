---
name: start-learning-db
description: Start ChromaDB container for the learning system
---

# Start Learning Database

Starts the ChromaDB Docker container for the learning aggregation system.

## Usage

```
/start-learning-db
```

## How It Works

When invoked via `Skill(skill="compound-learning:start-learning-db")`, the skill automatically executes and starts the container.

## Output Format

**Success:**
```
Learning database started successfully

ChromaDB is running on http://localhost:8000
Container: claude-learning-db

Ready to use /compound to create new learnings!
```

**Already running:**
```
Learning database is already running

ChromaDB: http://localhost:8000
Container: claude-learning-db

No action needed.
```

**Error:**
```
Failed to start learning database

Check Docker:
  docker logs claude-learning-db

Ensure Docker is running: docker ps
```

## Notes

- Requires Docker to be installed and running
- Default port is 8000 (configurable via .claude-plugin/config.json)
- Data persists in ~/.claude/chroma-data/ by default
