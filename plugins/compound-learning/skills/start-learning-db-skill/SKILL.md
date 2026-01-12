---
name: start-learning-db-skill
description: Start ChromaDB and index learning files
allowed-tools: Bash
---

# Start Learning Database

Starts the ChromaDB Docker container (if not running) and indexes all learning markdown files.

## Usage

Run the script from this skill's base directory:

```bash
python3 {BASE_DIR}/start-and-index.py
```

Where `{BASE_DIR}` is the path shown in "Base directory for this skill:" header above.

## Example

If the base directory is `/home/ubuntu/.claude/plugins/cache/connsulting-plugins/compound-learning/0.1.0/skills/start-learning-db-skill`, run:

```bash
python3 /home/ubuntu/.claude/plugins/cache/connsulting-plugins/compound-learning/0.1.0/skills/start-learning-db-skill/start-and-index.py
```

## Output

The script outputs indexed file counts:
```
Indexed 80 learning files:
  Global: 40
  Repos:
    - repo-name: 40
```

## Important

- Run ONLY the python3 command above
- Do NOT run docker commands yourself
- Do NOT search for files yourself
- The script handles everything
