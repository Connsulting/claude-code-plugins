#!/usr/bin/env python3
"""
Index all learning markdown files into ChromaDB
Runs directly without using MCP (avoids token waste)

Also generates a manifest summarizing learnings by topic for CLAUDE.md integration.
"""

import chromadb
from pathlib import Path
import hashlib
import re
import sys
import json
import os
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Set


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

    Note: Resolves symlinks to canonical paths to handle systems where
    home directory contains symlinks to other locations (e.g., /home/ubuntu/git -> /workspace/git)
    """
    # Get paths from config and resolve to canonical form
    global_dir = Path(config['learnings']['globalDir']).resolve()
    repo_search_path = Path(config['learnings']['repoSearchPath']).resolve()

    # Exclude patterns
    exclude_dirs = {
        'node_modules', '.git', '.venv', 'venv', '.cache',
        'build', 'dist', '__pycache__', '.next', '.nuxt'
    }

    global_files = []
    repo_files_by_name: Dict[str, List[Path]] = {}
    seen_canonical_paths: set = set()  # Track seen files to avoid duplicates

    # Search global learnings directory
    if global_dir.exists():
        md_files = list(global_dir.glob('**/*.md'))
        for f in md_files:
            canonical = f.resolve()
            if canonical not in seen_canonical_paths:
                seen_canonical_paths.add(canonical)
                global_files.append(canonical)

    # Build list of search paths - include both configured path and resolved path
    # This handles cases where symlinks point to different locations
    search_paths = {repo_search_path}

    # If globalDir resolved to a different root, also search from there
    # e.g., if /home/ubuntu/.projects -> /workspace/.projects, also search /workspace
    if global_dir.parts[1:2] and global_dir.parts[1] != repo_search_path.parts[1]:
        # Different root (e.g., /workspace vs /home)
        alt_search = Path('/' + global_dir.parts[1])
        if alt_search.exists():
            search_paths.add(alt_search)

    # Search for .projects/learnings directories under all search paths
    for search_path in search_paths:
        if not search_path.exists():
            continue

        for learnings_dir in search_path.rglob('.projects/learnings'):
            # Resolve to canonical path
            learnings_dir = learnings_dir.resolve()

            # Skip if this is the global dir (already handled above)
            if str(learnings_dir).startswith(str(global_dir)):
                continue

            # Skip excluded directories
            if any(excluded in learnings_dir.parts for excluded in exclude_dirs):
                continue

            # Extract repo name: learnings_dir is [repo]/.projects/learnings
            repo_name = learnings_dir.parent.parent.name

            # Find all .md files in this directory
            md_files = list(learnings_dir.glob('**/*.md'))

            for f in md_files:
                canonical = f.resolve()
                if canonical not in seen_canonical_paths:
                    seen_canonical_paths.add(canonical)
                    if repo_name not in repo_files_by_name:
                        repo_files_by_name[repo_name] = []
                    repo_files_by_name[repo_name].append(canonical)

    return global_files, repo_files_by_name


def extract_metadata_from_path(file_path: Path, config: Dict[str, Any]) -> Dict[str, str]:
    """Extract scope metadata from file path

    Uses canonical (resolved) paths to handle symlinks correctly.
    This ensures /home/ubuntu/git/... and /workspace/git/... are treated as the same file.
    """
    # Resolve symlinks to get canonical path
    canonical_path = file_path.resolve()
    path_str = str(canonical_path)

    # Also resolve globalDir to handle symlinked config paths
    global_dir = str(Path(config['learnings']['globalDir']).resolve())

    # Global learnings: configured globalDir
    if path_str.startswith(global_dir):
        return {
            "scope": "global",
            "repo": "",
            "file_path": path_str
        }

    # Repo learnings: extract repo name from directory containing .projects
    # Path structure: [repo]/.projects/learnings/file.md
    # canonical_path.parent = learnings/
    # canonical_path.parent.parent = .projects/
    # canonical_path.parent.parent.parent = [repo]/
    try:
        repo_name = canonical_path.parent.parent.parent.name
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


def extract_explicit_topic(content: str) -> str | None:
    """Extract explicit **Topic:** field from learning content"""
    # Match **Topic:** or **topic:** followed by the value
    match = re.search(r'\*\*[Tt]opic:\*\*\s*(.+?)(?:\n|$)', content)
    if match:
        return match.group(1).strip().lower().replace(' ', '-')
    return None


def detect_topic(content: str) -> str:
    """
    Detect topic from learning content.

    Priority:
    1. Explicit **Topic:** field in content
    2. More granular topic detection based on keywords
    3. Fallback to "other"
    """
    # First check for explicit topic
    explicit = extract_explicit_topic(content)
    if explicit:
        return explicit

    content_lower = content.lower()

    # Topic detection with more granular categories than detect_category
    # Order matters - more specific matches first
    topic_patterns = [
        # Authentication & Security
        ('authentication', ['jwt', 'oauth', 'login', 'session', 'refresh token', 'auth flow']),
        ('security', ['xss', 'cors', 'csrf', 'injection', 'sanitiz', 'vulnerability', 'credential']),

        # Error handling
        ('error-handling', ['retry', 'timeout', 'graceful', 'fallback', 'exception', 'error handling']),

        # Testing
        ('testing', ['mock', 'fixture', 'test case', 'integration test', 'unit test', 'e2e', 'assertion']),

        # Performance
        ('performance', ['caching', 'n+1', 'lazy load', 'optimize', 'bottleneck', 'profil']),

        # Deployment & DevOps
        ('deployment', ['docker', 'kubernetes', 'k8s', 'ci/cd', 'pipeline', 'deploy']),
        ('configuration', ['env var', 'environment', 'config', '.env', 'settings']),

        # Database
        ('database', ['migration', 'index', 'transaction', 'query', 'sql', 'orm']),

        # API
        ('api-integration', ['rest', 'graphql', 'endpoint', 'webhook', 'api call']),

        # Architecture
        ('architecture', ['pattern', 'design', 'structure', 'refactor', 'abstraction']),

        # Frontend
        ('frontend', ['component', 'render', 'state', 'css', 'style', 'layout']),

        # Memory/Storage
        ('memory-system', ['chromadb', 'vector', 'embedding', 'index', 'storage']),
    ]

    for topic, keywords in topic_patterns:
        if any(kw in content_lower for kw in keywords):
            return topic

    return 'other'


def extract_keywords(content: str) -> List[str]:
    """
    Extract significant keywords from learning content for manifest.
    Returns top keywords that help identify the learning's focus.
    """
    content_lower = content.lower()

    # Remove markdown formatting
    content_clean = re.sub(r'```.*?```', '', content_lower, flags=re.DOTALL)  # Remove code blocks
    content_clean = re.sub(r'[#*`\[\]()]', ' ', content_clean)  # Remove markdown chars
    content_clean = re.sub(r'https?://\S+', '', content_clean)  # Remove URLs

    # Significant technical keywords to look for
    significant_keywords = {
        # Auth
        'jwt', 'oauth', 'token', 'session', 'cookie', 'refresh', 'authentication', 'authorization',
        # Security
        'cors', 'xss', 'csrf', 'injection', 'sanitize', 'validate', 'credential', 'secret',
        # Error handling
        'retry', 'timeout', 'fallback', 'graceful', 'degradation', 'exception', 'error',
        # Testing
        'mock', 'fixture', 'stub', 'spy', 'assertion', 'integration', 'unit', 'e2e', 'coverage',
        # Performance
        'cache', 'caching', 'lazy', 'eager', 'optimization', 'bottleneck', 'profiling', 'n+1',
        # Database
        'migration', 'index', 'transaction', 'query', 'sql', 'orm', 'schema', 'foreign key',
        # API
        'rest', 'graphql', 'endpoint', 'webhook', 'request', 'response', 'payload',
        # Deployment
        'docker', 'kubernetes', 'container', 'ci/cd', 'pipeline', 'deploy', 'environment',
        # Frontend
        'component', 'render', 'state', 'hook', 'effect', 'props', 'context',
        # Architecture
        'pattern', 'singleton', 'factory', 'middleware', 'decorator', 'abstraction',
        # Technologies
        'react', 'vue', 'angular', 'express', 'fastapi', 'django', 'flask',
        'postgres', 'mysql', 'mongodb', 'redis', 'elasticsearch',
        'aws', 'gcp', 'azure', 'terraform', 'ansible',
        # Types
        'gotcha', 'workaround', 'pitfall', 'caveat', 'edge case',
    }

    # Find which significant keywords appear in content
    found_keywords = []
    for keyword in significant_keywords:
        # Match whole word (with some flexibility for plurals)
        pattern = r'\b' + re.escape(keyword) + r's?\b'
        if re.search(pattern, content_clean):
            found_keywords.append(keyword)

    return found_keywords[:8]  # Return top 8 keywords


def detect_learning_type(content: str) -> str | None:
    """
    Detect if this is a correction-type learning (don't do X).
    Returns 'correction' if detected, None otherwise.
    """
    content_lower = content.lower()

    correction_indicators = [
        "don't ", "dont ", "do not ", "never ", "avoid ",
        "mistake", "wrong", "incorrect", "pitfall", "gotcha",
        "instead of", "not working", "doesn't work", "broken"
    ]

    if any(indicator in content_lower for indicator in correction_indicators):
        return 'correction'
    return None


def extract_summary(content: str) -> str:
    """Extract first meaningful line as summary"""
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        # Skip headers, empty lines
        if line and not line.startswith('#') and len(line) > 10:
            return line[:200]  # First 200 chars
    return "Learning document"


def generate_manifest(manifest_data: Dict[str, List[Dict[str, Any]]], config: Dict[str, Any]) -> None:
    """
    Generate manifest files summarizing learnings by topic.

    Creates:
    - ~/.projects/learnings/MANIFEST.md (global manifest with all learnings)
    - [repo]/.projects/learnings/MANIFEST.md (repo-specific manifests)
    """
    print("\nGenerating manifest files...")

    global_dir = Path(config['learnings']['globalDir'])
    generated_count = 0

    # Aggregate all learnings for global manifest
    all_learnings = []
    for scope, learnings in manifest_data.items():
        for learning in learnings:
            learning['scope'] = scope
        all_learnings.extend(learnings)

    # Generate global manifest (includes everything)
    if all_learnings:
        global_manifest_path = global_dir / 'MANIFEST.md'
        _write_manifest_file(global_manifest_path, manifest_data, is_global=True)
        generated_count += 1
        print(f"  Generated: {global_manifest_path}")

    # Generate repo-specific manifests
    for scope, learnings in manifest_data.items():
        if scope == 'global':
            continue  # Skip global scope for repo manifests

        # Find the repo's learnings directory from file paths
        if learnings:
            sample_path = Path(learnings[0]['file_path'])
            repo_learnings_dir = sample_path.parent
            repo_manifest_path = repo_learnings_dir / 'MANIFEST.md'

            repo_data = {'repo': learnings}
            _write_manifest_file(repo_manifest_path, repo_data, is_global=False, repo_name=scope)
            generated_count += 1
            print(f"  Generated: {repo_manifest_path}")

    print(f"\n[OK] Generated {generated_count} manifest file(s)")


def _write_manifest_file(
    path: Path,
    data: Dict[str, List[Dict[str, Any]]],
    is_global: bool,
    repo_name: str | None = None
) -> None:
    """Write a manifest file with topic summaries."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# Learnings Manifest")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")

    if is_global:
        # Global manifest includes all scopes
        # First: Global learnings section
        global_learnings = data.get('global', [])
        if global_learnings:
            lines.extend(_format_scope_section("Global Learnings", global_learnings))

        # Then: Each repo's learnings
        for scope, learnings in sorted(data.items()):
            if scope == 'global':
                continue
            lines.extend(_format_scope_section(f"Repo Learnings: {scope}", learnings))
    else:
        # Repo-specific manifest
        learnings = data.get('repo', [])
        if learnings:
            lines.extend(_format_scope_section(f"Repo Learnings: {repo_name}", learnings))

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _format_scope_section(title: str, learnings: List[Dict[str, Any]]) -> List[str]:
    """Format a section of the manifest for a specific scope."""
    lines = []

    # Count corrections
    total = len(learnings)
    corrections = sum(1 for l in learnings if l.get('is_correction'))

    # Build title with correction count if any
    if corrections > 0:
        lines.append(f"## {title} ({total} total, {corrections} corrections)")
    else:
        lines.append(f"## {title} ({total} total)")
    lines.append("")

    # Aggregate by topic
    topic_data: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'count': 0,
        'keywords': defaultdict(int),
        'corrections': 0
    })

    for learning in learnings:
        topic = learning['topic']
        topic_data[topic]['count'] += 1
        if learning.get('is_correction'):
            topic_data[topic]['corrections'] += 1
        for kw in learning.get('keywords', []):
            topic_data[topic]['keywords'][kw] += 1

    # Sort topics by count (descending)
    sorted_topics = sorted(topic_data.items(), key=lambda x: x[1]['count'], reverse=True)

    # Create table
    lines.append("| Topic | Count | Sample Keywords |")
    lines.append("|-------|-------|-----------------|")

    for topic, data in sorted_topics:
        count = data['count']
        # Get top 4-6 keywords by frequency
        sorted_keywords = sorted(data['keywords'].items(), key=lambda x: x[1], reverse=True)
        top_keywords = [kw for kw, _ in sorted_keywords[:6]]
        keywords_str = ', '.join(top_keywords) if top_keywords else ''

        # Add correction indicator
        if data['corrections'] > 0:
            count_str = f"{count} ({data['corrections']}⚠️)"
        else:
            count_str = str(count)

        lines.append(f"| {topic} | {count_str} | {keywords_str} |")

    lines.append("")
    return lines


def rebuild_manifest_only(config: Dict[str, Any]) -> None:
    """
    Rebuild manifest from existing ChromaDB data without re-indexing files.
    """
    print("Loading configuration...")
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

    # Get all documents from ChromaDB
    print("\nFetching learnings from ChromaDB...")
    try:
        results = collection.get(include=['metadatas'])
        total = len(results['ids'])
        print(f"Found {total} documents in ChromaDB")

        if total == 0:
            print("\nNo learnings in database. Run /index-learnings first!")
            return

        # Build manifest data from ChromaDB metadata
        manifest_data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for metadata in results['metadatas']:
            scope = metadata.get('scope', 'global')
            repo = metadata.get('repo', '')
            scope_key = repo if scope == 'repo' and repo else 'global'

            # Parse keywords from comma-separated string
            keywords_str = metadata.get('keywords', '')
            keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]

            manifest_data[scope_key].append({
                'topic': metadata.get('topic', 'other'),
                'keywords': keywords,
                'is_correction': metadata.get('learning_type') == 'correction',
                'file_path': metadata.get('file_path', '')
            })

        # Generate manifests
        generate_manifest(manifest_data, config)

    except Exception as e:
        print(f"[ERROR] Failed to fetch from ChromaDB: {e}")
        sys.exit(1)


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

    # Track data for manifest generation
    manifest_data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for file_path in all_files:
        try:
            # Read file
            content = file_path.read_text(encoding='utf-8')

            # Extract metadata (uses canonical path internally)
            metadata = extract_metadata_from_path(file_path, config)
            metadata['tags'] = ','.join(auto_extract_tags(content))
            metadata['category'] = detect_category(content)
            metadata['summary'] = extract_summary(content)

            # New: Extract topic and keywords for manifest
            topic = detect_topic(content)
            keywords = extract_keywords(content)
            learning_type = detect_learning_type(content)

            metadata['topic'] = topic
            metadata['keywords'] = ','.join(keywords)
            if learning_type:
                metadata['learning_type'] = learning_type

            # Generate ID from canonical path to handle symlinks
            # This ensures the same file via different paths gets the same ID
            canonical_path = file_path.resolve()
            doc_id = hashlib.md5(str(canonical_path).encode()).hexdigest()

            # Add to ChromaDB
            collection.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata]
            )

            # Track for manifest
            scope_key = metadata['repo'] if metadata['scope'] == 'repo' else 'global'
            manifest_data[scope_key].append({
                'topic': topic,
                'keywords': keywords,
                'is_correction': learning_type == 'correction',
                'file_path': metadata['file_path']
            })

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

    # Generate manifest files
    if manifest_data:
        generate_manifest(manifest_data, config)


def main():
    """Main entry point with argument handling."""
    import argparse

    parser = argparse.ArgumentParser(description='Index learning files into ChromaDB')
    parser.add_argument(
        '--rebuild-manifest',
        action='store_true',
        help='Only rebuild manifest from existing ChromaDB data (skip file re-indexing)'
    )

    args = parser.parse_args()

    config = load_config()

    if args.rebuild_manifest:
        rebuild_manifest_only(config)
    else:
        index_learning_files()


if __name__ == '__main__':
    main()
