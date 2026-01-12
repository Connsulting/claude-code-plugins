#!/usr/bin/env python3
"""
Index all learning markdown files into ChromaDB
Runs directly without using MCP (avoids token waste)
"""

import chromadb
from pathlib import Path
import hashlib
import re
import sys
import json
import os
from typing import Dict, List, Tuple, Any


def load_config() -> Dict[str, Any]:
    """Load configuration from environment variables, then config file, then defaults."""
    home = os.path.expanduser('~')

    # Default configuration
    defaults = {
        'chromadb': {
            'host': 'localhost',
            'port': 8000,
            'dataDir': os.path.join(home, '.claude/chroma-data')
        },
        'learnings': {
            'globalDir': os.path.join(home, '.projects/learnings'),
            'repoSearchPath': home,
            'distanceThreshold': 0.5
        }
    }

    # Environment variables take highest priority (with validation)
    env_port = os.environ.get('CHROMADB_PORT')
    env_threshold = os.environ.get('LEARNINGS_DISTANCE_THRESHOLD')
    env_data_dir = os.environ.get('CHROMADB_DATA_DIR', '')
    env_global_dir = os.environ.get('LEARNINGS_GLOBAL_DIR', '')
    env_repo_path = os.environ.get('LEARNINGS_REPO_SEARCH_PATH', '')

    # Validate numeric environment variables
    parsed_port = None
    if env_port:
        try:
            parsed_port = int(env_port)
        except ValueError:
            print(f"Warning: Invalid CHROMADB_PORT '{env_port}', using default")

    parsed_threshold = None
    if env_threshold:
        try:
            parsed_threshold = float(env_threshold)
        except ValueError:
            print(f"Warning: Invalid LEARNINGS_DISTANCE_THRESHOLD '{env_threshold}', using default")

    env_config = {
        'chromadb': {
            'host': os.environ.get('CHROMADB_HOST'),
            'port': parsed_port,
            'dataDir': os.path.expanduser(env_data_dir) if env_data_dir else None
        },
        'learnings': {
            'globalDir': os.path.expanduser(env_global_dir) if env_global_dir else None,
            'repoSearchPath': os.path.expanduser(env_repo_path) if env_repo_path else None,
            'distanceThreshold': parsed_threshold
        }
    }

    # Try to load config file
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.join(home, '.claude'))
    config_file = Path(plugin_root) / '.claude-plugin' / 'config.json'
    file_config: Dict[str, Any] = {'chromadb': {}, 'learnings': {}}

    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                file_config = json.load(f)

            # Expand ${HOME} in paths
            for key in ['dataDir']:
                if key in file_config.get('chromadb', {}):
                    file_config['chromadb'][key] = file_config['chromadb'][key].replace('${HOME}', home)
            for key in ['globalDir', 'repoSearchPath']:
                if key in file_config.get('learnings', {}):
                    file_config['learnings'][key] = file_config['learnings'][key].replace('${HOME}', home)
        except Exception as e:
            print(f"Warning: Failed to load config from {config_file}: {e}")

    # Merge: defaults <- file_config <- env_config (env wins)
    result = defaults.copy()
    for section in ['chromadb', 'learnings']:
        if section in file_config:
            for key, value in file_config[section].items():
                if value is not None:
                    result[section][key] = value
        for key, value in env_config[section].items():
            if value is not None:
                result[section][key] = value

    return result


def find_all_learning_files(config: Dict[str, Any]) -> Tuple[List[Path], Dict[str, List[Path]]]:
    """Find all .projects/learnings directories in configured locations

    Returns:
        Tuple of (global_files, repo_files_by_name)
        where repo_files_by_name is a dict mapping repo name to list of files
    """
    # Get paths from config
    global_dir = Path(config['learnings']['globalDir'])
    repo_search_path = Path(config['learnings']['repoSearchPath'])

    # Exclude patterns
    exclude_dirs = {
        'node_modules', '.git', '.venv', 'venv', '.cache',
        'build', 'dist', '__pycache__', '.next', '.nuxt'
    }

    global_files = []
    repo_files_by_name: Dict[str, List[Path]] = {}

    # Search global learnings directory
    if global_dir.exists():
        md_files = list(global_dir.glob('**/*.md'))
        global_files.extend(md_files)

    # Search for .projects/learnings directories under repo search path
    if repo_search_path.exists():
        for learnings_dir in repo_search_path.rglob('.projects/learnings'):
            # Skip excluded directories
            if any(excluded in learnings_dir.parts for excluded in exclude_dirs):
                continue

            # Extract repo name: learnings_dir is [repo]/.projects/learnings
            repo_name = learnings_dir.parent.parent.name

            # Find all .md files in this directory
            md_files = list(learnings_dir.glob('**/*.md'))

            if md_files:
                if repo_name not in repo_files_by_name:
                    repo_files_by_name[repo_name] = []
                repo_files_by_name[repo_name].extend(md_files)

    return global_files, repo_files_by_name


def extract_metadata_from_path(file_path: Path, config: Dict[str, Any]) -> Dict[str, str]:
    """Extract scope metadata from file path"""
    path_str = str(file_path)
    global_dir = config['learnings']['globalDir']

    # Global learnings: configured globalDir
    if path_str.startswith(global_dir):
        return {
            "scope": "global",
            "repo": "",
            "file_path": path_str
        }

    # Repo learnings: extract repo name from directory containing .projects
    # Path structure: [repo]/.projects/learnings/file.md
    # file_path.parent = learnings/
    # file_path.parent.parent = .projects/
    # file_path.parent.parent.parent = [repo]/
    try:
        repo_name = file_path.parent.parent.parent.name
        return {
            "scope": "repo",
            "repo": repo_name,
            "file_path": path_str
        }
    except Exception:
        # Fallback for shallow paths
        return {
            "scope": "repo",
            "repo": "unknown",
            "file_path": path_str
        }


def auto_extract_tags(content: str) -> List[str]:
    """Auto-extract tags from markdown content"""
    tags = set()

    # Extract from code blocks
    code_blocks = re.findall(r'```(\w+)?\n(.*?)```', content, re.DOTALL)
    for lang, code in code_blocks:
        if lang:
            tags.add(lang.lower())

        # Detect common imports
        import_patterns = [
            r'import\s+(\w+)',
            r'from\s+(\w+)',
            r'require\([\'"](\w+)[\'"]\)'
        ]
        for pattern in import_patterns:
            imports = re.findall(pattern, code)
            tags.update(imp.lower() for imp in imports)

    # Detect technology keywords
    tech_keywords = [
        'docker', 'redis', 'postgres', 'mysql', 'mongodb',
        'jwt', 'oauth', 'react', 'vue', 'angular', 'express',
        'fastapi', 'django', 'flask', 'kubernetes', 'aws',
        'terraform', 'ansible', 'jenkins', 'github', 'gitlab'
    ]
    for keyword in tech_keywords:
        if keyword.lower() in content.lower():
            tags.add(keyword)

    return list(tags)


def detect_category(content: str) -> str:
    """Auto-detect category from content"""
    content_lower = content.lower()

    if any(word in content_lower for word in ['auth', 'token', 'password', 'encryption', 'security']):
        return 'security'
    elif any(word in content_lower for word in ['test', 'debugging', 'error', 'bug']):
        return 'debugging'
    elif any(word in content_lower for word in ['performance', 'slow', 'optimization', 'cache']):
        return 'performance'
    elif any(word in content_lower for word in ['deploy', 'ci/cd', 'pipeline', 'docker']):
        return 'deployment'
    elif any(word in content_lower for word in ['database', 'sql', 'query', 'migration']):
        return 'database'
    else:
        return 'general'


def extract_summary(content: str) -> str:
    """Extract first meaningful line as summary"""
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        # Skip headers, empty lines
        if line and not line.startswith('#') and len(line) > 10:
            return line[:200]  # First 200 chars
    return "Learning document"


def index_learning_files():
    """Index all learning files into ChromaDB"""
    # Load configuration
    print("Loading configuration...")
    config = load_config()
    host = config['chromadb']['host']
    port = config['chromadb']['port']

    print("Connecting to ChromaDB...")
    try:
        client = chromadb.HttpClient(host=host, port=port)
        collection = client.get_or_create_collection(
            name="learnings",
            metadata={"hnsw:space": "cosine"}
        )
        print(f"[OK] Connected to ChromaDB at {host}:{port}")
    except Exception as e:
        print(f"[ERROR] Failed to connect to ChromaDB: {e}")
        print("  Make sure ChromaDB is running: /start-learning-db")
        sys.exit(1)

    print("\nDiscovering learning files...")
    global_files, repo_files_by_name = find_all_learning_files(config)

    # Flatten repo files for processing
    repo_files = [f for files in repo_files_by_name.values() for f in files]
    all_files = global_files + repo_files
    total = len(all_files)

    print(f"\nFound {total} learning files:")
    print(f"  Global: {len(global_files)}")
    if repo_files_by_name:
        print(f"  Repos:")
        for repo_name in sorted(repo_files_by_name.keys()):
            print(f"    - {repo_name}: {len(repo_files_by_name[repo_name])}")
    else:
        print(f"  Repos: 0")

    if total == 0:
        print("\nNo learning files found. Run /compound to create some!")
        return

    print("\nIndexing into ChromaDB...")
    indexed = 0
    errors = 0

    for file_path in all_files:
        try:
            # Read file
            content = file_path.read_text(encoding='utf-8')

            # Extract metadata
            metadata = extract_metadata_from_path(file_path, config)
            metadata['tags'] = ','.join(auto_extract_tags(content))
            metadata['category'] = detect_category(content)
            metadata['summary'] = extract_summary(content)

            # Generate ID from file path
            doc_id = hashlib.md5(str(file_path).encode()).hexdigest()

            # Add to ChromaDB
            collection.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata]
            )

            indexed += 1
            if indexed % 10 == 0:
                print(f"  Indexed {indexed}/{total}...")

        except Exception as e:
            print(f"  [ERROR] Error indexing {file_path}: {e}")
            errors += 1

    print(f"\n[OK] Indexing complete!")
    print(f"  Successfully indexed: {indexed}")
    if errors > 0:
        print(f"  Errors: {errors}")

    # Get collection stats
    try:
        count = collection.count()
        print(f"\nTotal documents in ChromaDB: {count}")
    except Exception:
        pass


if __name__ == '__main__':
    index_learning_files()
