---
description: Start ChromaDB container and prepare learning database for the day
---

# Start Learning Database

Start the ChromaDB Docker container for the learning aggregation system.

## Usage

```
/start-learnings
```

This command:
1. Starts the ChromaDB Docker container
2. Waits for it to be ready
3. Discovers existing learning markdown files
4. Reports what needs indexing
5. Confirms the database is accessible

Run this once per day when you boot your dev machine to ensure the learning system is ready.

## Your Task

1. `Skill(skill="compound-learning:start-learning-db")` - Start ChromaDB container
2. `Skill(skill="compound-learning:index-learnings")` - Index learning files
3. Report status to user

### Report to User

Provide a concise status report:

**If successful:**
```
[OK] Learning database started successfully

ChromaDB is running on http://localhost:8000
Container: claude-learning-db
Storage: ~/.claude/chroma-data/

Indexed learning files:
- Global:  X files
- Client:  Y files
- Repo:    Z files
Total:     N documents in ChromaDB

Ready to use /compound to create new learnings!
```

**If already running:**
```
[INFO] Learning database is already running

ChromaDB: http://localhost:8000
Container: claude-learning-db (up X minutes)

No action needed. Ready to use /compound!
```

**If failed:**
```
[ERROR] Failed to start learning database

Check Docker:
  docker logs claude-learning-db

Try: /start-learning-db
```

## Important Notes

- This command starts the container, discovers, and indexes all existing learning files
- Future learnings are automatically indexed when you run /compound
- If you manually edit markdown files, run /start-learning-db again to re-index

## Troubleshooting

**Container won't start:**
- Check Docker is running: `docker ps`
- Check port 8000 is free: `lsof -i :8000`
- View logs: `docker logs claude-learning-db`

**ChromaDB not accessible:**
- Verify container is running: `docker ps | grep claude-learning-db`
- Check container is accessible: `curl http://localhost:8000/api/v1`
