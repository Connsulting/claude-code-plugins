#!/usr/bin/env python3
"""
Combined script: Start ChromaDB (if needed) and index all learning files.
Only outputs the final summary to minimize token usage.
"""

import subprocess
import sys
import time
import hashlib
import re
import os
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any


def check_docker_running() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_chromadb_container(config: Dict[str, Any]) -> Tuple[bool, str]:
    """Ensure ChromaDB container is running and healthy.

    Returns (success, message)
    """
    container_name = "claude-learning-db"
    port = config['chromadb']['port']
    data_dir = config['chromadb']['dataDir']

    # Check if Docker is running
    if not check_docker_running():
        return False, "Docker is not running. Please start Docker first."

    # Create data directory if needed
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # Check if container exists
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True
    )
    container_exists = container_name in result.stdout.split('\n')

    # Check if container is running
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True
    )
    container_running = container_name in result.stdout.split('\n')

    if container_running:
        # Check if healthy by trying to connect (any HTTP response means it's up)
        try:
            import urllib.request
            import urllib.error
            try:
                urllib.request.urlopen(f"http://localhost:{port}/", timeout=5)
            except urllib.error.HTTPError:
                pass  # 404 is fine, server is responding
            return True, "already running"
        except Exception:
            # Running but not responding, restart it
            subprocess.run(["docker", "restart", container_name], capture_output=True)
            time.sleep(3)
    elif container_exists:
        # Exists but not running, start it
        subprocess.run(["docker", "start", container_name], capture_output=True)
        time.sleep(3)
    else:
        # Create new container
        subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{port}:8000",
            "-v", f"{data_dir}:/data",
            "-e", "IS_PERSISTENT=TRUE",
            "-e", "PERSIST_DIRECTORY=/data",
            "-e", "ANONYMIZED_TELEMETRY=FALSE",
            "-e", "ALLOW_RESET=TRUE",
            "--restart", "unless-stopped",
            "chromadb/chroma:latest"
        ], capture_output=True)
        time.sleep(5)

    # Verify it's accessible (any HTTP response means it's up)
    for _ in range(5):
        try:
            import urllib.request
            import urllib.error
            try:
                urllib.request.urlopen(f"http://localhost:{port}/", timeout=5)
            except urllib.error.HTTPError:
                pass  # 404 is fine, server is responding
            return True, "started"
        except Exception:
            time.sleep(2)

    return False, "Container started but ChromaDB not responding"


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, config file, or defaults."""
    home = os.path.expanduser('~')

    defaults = {
        'chromadb': {
            'host': 'localhost',
            'port': 8000,
            'dataDir': os.path.join(home, '.claude/chroma-data')
        },
        'learnings': {
            'globalDir': os.path.join(home, '.projects/learnings'),
            'repoSearchPath': home
        }
    }

    # Environment variables
    env_port = os.environ.get('CHROMADB_PORT')
    env_data_dir = os.environ.get('CHROMADB_DATA_DIR', '')
    env_global_dir = os.environ.get('LEARNINGS_GLOBAL_DIR', '')
    env_repo_path = os.environ.get('LEARNINGS_REPO_SEARCH_PATH', '')

    parsed_port = None
    if env_port:
        try:
            parsed_port = int(env_port)
        except ValueError:
            pass

    # Try config file
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.join(home, '.claude'))
    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)
            for key in ['dataDir']:
                if key in file_config.get('chromadb', {}):
                    file_config['chromadb'][key] = file_config['chromadb'][key].replace('${HOME}', home)
            for key in ['globalDir', 'repoSearchPath']:
                if key in file_config.get('learnings', {}):
                    file_config['learnings'][key] = file_config['learnings'][key].replace('${HOME}', home)
            # Merge
            for section in ['chromadb', 'learnings']:
                if section in file_config:
                    for k, v in file_config[section].items():
                        if v is not None:
                            defaults[section][k] = v
        except Exception:
            pass

    # Apply env overrides
    if parsed_port:
        defaults['chromadb']['port'] = parsed_port
    if env_data_dir:
        defaults['chromadb']['dataDir'] = os.path.expanduser(env_data_dir)
    if env_global_dir:
        defaults['learnings']['globalDir'] = os.path.expanduser(env_global_dir)
    if env_repo_path:
        defaults['learnings']['repoSearchPath'] = os.path.expanduser(env_repo_path)

    return defaults


def find_learning_files(config: Dict[str, Any]) -> Tuple[List[Path], Dict[str, List[Path]]]:
    """Find all learning markdown files."""
    global_dir = Path(config['learnings']['globalDir']).resolve()
    repo_search_path = Path(config['learnings']['repoSearchPath']).resolve()

    exclude_dirs = {
        'node_modules', '.git', '.venv', 'venv', '.cache',
        'build', 'dist', '__pycache__', '.next', '.nuxt'
    }

    global_files = []
    repo_files_by_name: Dict[str, List[Path]] = {}

    if global_dir.exists():
        global_files.extend(global_dir.glob('**/*.md'))

    if repo_search_path.exists():
        # Walk directory tree manually to follow symlinks
        for root, dirs, files in os.walk(repo_search_path, followlinks=True):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_dirs]

            root_path = Path(root)
            if root_path.name == 'learnings' and root_path.parent.name == '.projects':
                learnings_dir = root_path.resolve()

                # Skip if this is the global directory
                if learnings_dir == global_dir:
                    continue

                # Extract repo name: [repo]/.projects/learnings
                repo_name = learnings_dir.parent.parent.name
                md_files = list(learnings_dir.glob('**/*.md'))

                if md_files:
                    if repo_name not in repo_files_by_name:
                        repo_files_by_name[repo_name] = []
                    repo_files_by_name[repo_name].extend(md_files)

    return list(global_files), repo_files_by_name


def extract_metadata(file_path: Path, content: str, config: Dict[str, Any]) -> Dict[str, str]:
    """Extract metadata from file."""
    path_str = str(file_path.resolve())
    global_dir = str(Path(config['learnings']['globalDir']).resolve())

    # Scope
    if path_str.startswith(global_dir):
        scope = "global"
        repo = ""
    else:
        scope = "repo"
        try:
            repo = file_path.parent.parent.parent.name
        except Exception:
            repo = "unknown"

    # Auto-extract tags
    tags = set()
    code_blocks = re.findall(r'```(\w+)?\n', content)
    tags.update(lang.lower() for lang in code_blocks if lang)

    tech_keywords = ['docker', 'redis', 'postgres', 'jwt', 'oauth', 'react', 'kubernetes', 'aws']
    for kw in tech_keywords:
        if kw.lower() in content.lower():
            tags.add(kw)

    # Category
    content_lower = content.lower()
    if any(w in content_lower for w in ['auth', 'token', 'password', 'security']):
        category = 'security'
    elif any(w in content_lower for w in ['test', 'debug', 'error', 'bug']):
        category = 'debugging'
    elif any(w in content_lower for w in ['performance', 'slow', 'cache']):
        category = 'performance'
    else:
        category = 'general'

    # Summary
    summary = "Learning document"
    for line in content.split('\n'):
        line = line.strip()
        if line and not line.startswith('#') and len(line) > 10:
            summary = line[:200]
            break

    return {
        "scope": scope,
        "repo": repo,
        "file_path": path_str,
        "tags": ','.join(tags),
        "category": category,
        "summary": summary
    }


def index_files(config: Dict[str, Any]) -> Tuple[int, int, int, Dict[str, int]]:
    """Index all files into ChromaDB.

    Returns (global_count, repo_total, errors, repo_breakdown)
    """
    try:
        import chromadb
    except ImportError:
        print("[ERROR] chromadb not installed. Run: pip install chromadb")
        sys.exit(1)

    host = config['chromadb']['host']
    port = config['chromadb']['port']

    try:
        client = chromadb.HttpClient(host=host, port=port)
        collection = client.get_or_create_collection(
            name="learnings",
            metadata={"hnsw:space": "cosine"}
        )
    except Exception as e:
        print(f"[ERROR] Failed to connect to ChromaDB: {e}")
        sys.exit(1)

    global_files, repo_files_by_name = find_learning_files(config)

    errors = 0
    indexed_global = 0
    repo_counts: Dict[str, int] = {}

    # Index global files
    for file_path in global_files:
        try:
            content = file_path.read_text(encoding='utf-8')
            metadata = extract_metadata(file_path, content, config)
            doc_id = hashlib.md5(str(file_path).encode()).hexdigest()
            collection.upsert(ids=[doc_id], documents=[content], metadatas=[metadata])
            indexed_global += 1
        except Exception:
            errors += 1

    # Index repo files
    for repo_name, files in repo_files_by_name.items():
        repo_counts[repo_name] = 0
        for file_path in files:
            try:
                content = file_path.read_text(encoding='utf-8')
                metadata = extract_metadata(file_path, content, config)
                doc_id = hashlib.md5(str(file_path).encode()).hexdigest()
                collection.upsert(ids=[doc_id], documents=[content], metadatas=[metadata])
                repo_counts[repo_name] += 1
            except Exception:
                errors += 1

    repo_total = sum(repo_counts.values())
    return indexed_global, repo_total, errors, repo_counts


def main():
    config = load_config()

    # Ensure ChromaDB is running
    success, status = ensure_chromadb_container(config)
    if not success:
        print(f"[ERROR] {status}")
        sys.exit(1)

    # Index files
    global_count, repo_total, errors, repo_breakdown = index_files(config)
    total = global_count + repo_total

    # Output final summary only
    print(f"Indexed {total} learning files:")
    print(f"  Global: {global_count}")
    if repo_breakdown:
        print(f"  Repos:")
        for repo_name in sorted(repo_breakdown.keys()):
            print(f"    - {repo_name}: {repo_breakdown[repo_name]}")
    else:
        print(f"  Repos: 0")

    if errors > 0:
        print(f"  Errors: {errors}")

    if total == 0:
        print("\nNo learning files found. Run /compound to create some!")


if __name__ == '__main__':
    main()
