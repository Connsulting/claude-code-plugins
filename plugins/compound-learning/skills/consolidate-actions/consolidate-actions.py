#!/usr/bin/env python3
"""
Execute consolidation actions: merge, archive, delete, rescope, get.
All destructive operations create backups first.
"""

import sys
import os

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT', os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _PLUGIN_ROOT)

import json
import shutil
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional

import lib.db as db


def create_backup(file_path: str, archive_dir: str) -> Optional[str]:
    """Create a backup of a file before destructive operation."""
    if not os.path.exists(file_path):
        return None

    date_dir = datetime.now().strftime('%Y-%m-%d')
    backup_dir = os.path.join(archive_dir, date_dir)
    os.makedirs(backup_dir, exist_ok=True)

    filename = os.path.basename(file_path)
    backup_path = os.path.join(backup_dir, filename)

    counter = 1
    while os.path.exists(backup_path):
        name, ext = os.path.splitext(filename)
        backup_path = os.path.join(backup_dir, f"{name}_{counter}{ext}")
        counter += 1

    shutil.copy2(file_path, backup_path)
    return backup_path


def action_get(ids: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch full content for specified document IDs."""
    try:
        conn = db.get_connection(config)
        results = db.get_documents_by_ids(conn, ids)
        conn.close()

        if not results or not results.get('ids'):
            return {'status': 'error', 'message': 'No documents found for provided IDs'}

        documents = []
        for i, doc_id in enumerate(results['ids']):
            documents.append({
                'id': doc_id,
                'content': results['documents'][i] if results.get('documents') else '',
                'metadata': results['metadatas'][i] if results.get('metadatas') else {}
            })

        return {'status': 'success', 'documents': documents}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def action_delete(ids: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    """Delete learnings with backup."""
    archive_dir = config['learnings']['archiveDir']
    results = {'status': 'success', 'deleted': [], 'backed_up': [], 'errors': []}

    try:
        conn = db.get_connection(config)
        docs = db.get_documents_by_ids(conn, ids)

        if not docs or not docs.get('ids'):
            conn.close()
            return {'status': 'error', 'message': 'No documents found for provided IDs'}

        for i, doc_id in enumerate(docs['ids']):
            metadata = docs['metadatas'][i] if docs.get('metadatas') else {}
            file_path = metadata.get('file_path', '')

            if file_path and os.path.exists(file_path):
                backup_path = create_backup(file_path, archive_dir)
                if backup_path:
                    results['backed_up'].append({'original': file_path, 'backup': backup_path})
                os.remove(file_path)

            db.delete_document(conn, doc_id)
            results['deleted'].append(doc_id)

        conn.close()
        return results
    except Exception as e:
        results['status'] = 'error'
        results['errors'].append(str(e))
        return results


def action_archive(ids: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    """Move learnings to archive directory."""
    archive_dir = config['learnings']['archiveDir']
    date_dir = datetime.now().strftime('%Y-%m-%d')
    target_dir = os.path.join(archive_dir, date_dir)
    os.makedirs(target_dir, exist_ok=True)

    results = {'status': 'success', 'archived': [], 'errors': []}

    try:
        conn = db.get_connection(config)
        docs = db.get_documents_by_ids(conn, ids)

        if not docs or not docs.get('ids'):
            conn.close()
            return {'status': 'error', 'message': 'No documents found for provided IDs'}

        for i, doc_id in enumerate(docs['ids']):
            metadata = docs['metadatas'][i] if docs.get('metadatas') else {}
            file_path = metadata.get('file_path', '')

            if file_path and os.path.exists(file_path):
                filename = os.path.basename(file_path)
                archive_path = os.path.join(target_dir, filename)

                counter = 1
                while os.path.exists(archive_path):
                    name, ext = os.path.splitext(filename)
                    archive_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
                    counter += 1

                shutil.move(file_path, archive_path)
                db.delete_document(conn, doc_id)
                results['archived'].append({'original': file_path, 'archived_to': archive_path})
            else:
                db.delete_document(conn, doc_id)
                results['archived'].append({'id': doc_id, 'note': 'Removed from DB only (file not found)'})

        conn.close()
        return results
    except Exception as e:
        results['status'] = 'error'
        results['errors'].append(str(e))
        return results


def action_rescope(doc_id: str, new_scope: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Move a learning between global and repo scope."""
    global_dir = config['learnings']['globalDir']
    archive_dir = config['learnings']['archiveDir']

    try:
        conn = db.get_connection(config)
        docs = db.get_documents_by_ids(conn, [doc_id])

        if not docs or not docs.get('ids'):
            conn.close()
            return {'status': 'error', 'message': f'Document not found: {doc_id}'}

        content = docs['documents'][0] if docs.get('documents') else ''
        metadata = docs['metadatas'][0] if docs.get('metadatas') else {}
        old_file_path = metadata.get('file_path', '')
        current_scope = metadata.get('scope', '')

        if current_scope == new_scope:
            conn.close()
            return {'status': 'error', 'message': f'Document already has scope: {new_scope}'}

        filename = os.path.basename(old_file_path) if old_file_path else f'learning-{doc_id[:8]}.md'

        if new_scope == 'global':
            new_dir = global_dir
        else:
            conn.close()
            return {'status': 'error', 'message': 'Rescoping to repo requires specifying target repo path'}

        os.makedirs(new_dir, exist_ok=True)
        new_file_path = os.path.join(new_dir, filename)

        counter = 1
        while os.path.exists(new_file_path):
            name, ext = os.path.splitext(filename)
            new_file_path = os.path.join(new_dir, f"{name}_{counter}{ext}")
            counter += 1

        if old_file_path and os.path.exists(old_file_path):
            create_backup(old_file_path, archive_dir)
            shutil.move(old_file_path, new_file_path)
        else:
            with open(new_file_path, 'w', encoding='utf-8') as f:
                f.write(content)

        new_metadata = metadata.copy()
        new_metadata['scope'] = new_scope
        new_metadata['file_path'] = new_file_path
        if new_scope == 'global':
            new_metadata['repo'] = ''

        new_doc_id = hashlib.md5(new_file_path.encode()).hexdigest()

        db.delete_document(conn, doc_id)
        db.upsert_document(conn, new_doc_id, content, new_metadata)
        conn.close()

        return {
            'status': 'success',
            'old_path': old_file_path,
            'new_path': new_file_path,
            'old_id': doc_id,
            'new_id': new_doc_id,
            'new_scope': new_scope
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def action_merge(ids: List[str], name: str, config: Dict[str, Any],
                 output_dir: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Merge multiple learnings into one.

    Args:
        ids: Document IDs to merge
        name: Name for merged file (kebab-case)
        config: Configuration dict
        output_dir: Override output directory (bypasses scope logic)
        dry_run: If True, show what would happen without executing
    """
    archive_dir = config['learnings']['archiveDir']

    try:
        conn = db.get_connection(config)
        docs = db.get_documents_by_ids(conn, ids)

        if not docs or not docs.get('ids') or len(docs['ids']) < 2:
            conn.close()
            return {'status': 'error', 'message': 'Need at least 2 documents to merge'}

        contents = []
        metadatas = []
        file_paths = []
        scopes = set()

        for i, doc_id in enumerate(docs['ids']):
            content = docs['documents'][i] if docs.get('documents') else ''
            metadata = docs['metadatas'][i] if docs.get('metadatas') else {}
            contents.append(content)
            metadatas.append(metadata)
            file_paths.append(metadata.get('file_path', ''))
            scopes.add(metadata.get('scope', 'repo'))

        # Output directory: explicit override > scope-based logic
        if output_dir:
            merged_dir = output_dir
            global_dir = config['learnings']['globalDir']
            merged_scope = 'global' if merged_dir.startswith(global_dir) else 'repo'
        else:
            merged_scope = 'global' if 'global' in scopes else 'repo'
            if merged_scope == 'global':
                merged_dir = config['learnings']['globalDir']
            else:
                first_path = file_paths[0]
                if first_path:
                    merged_dir = os.path.dirname(first_path)
                else:
                    merged_dir = config['learnings']['globalDir']

        os.makedirs(merged_dir, exist_ok=True)

        date_str = datetime.now().strftime('%Y-%m-%d')
        merged_filename = f"{name}-{date_str}.md"
        merged_path = os.path.join(merged_dir, merged_filename)

        counter = 1
        while os.path.exists(merged_path):
            merged_path = os.path.join(merged_dir, f"{name}-{date_str}-{counter}.md")
            counter += 1

        # Collect tags and topics from source documents
        all_tags = set()
        all_topics = set()
        for m in metadatas:
            tags_str = m.get('tags', '') or m.get('keywords', '')
            if tags_str:
                all_tags.update(t.strip() for t in tags_str.split(',') if t.strip())
            topic = m.get('topic', '')
            if topic and topic != 'other':
                all_topics.add(topic)

        merged_topic = list(all_topics)[0] if all_topics else name.split('-')[0]
        merged_tags = ', '.join(sorted(all_tags)[:8]) if all_tags else name.replace('-', ', ')

        # Build structured merged content
        merged_content = f"# {name.replace('-', ' ').title()}\n\n"
        merged_content += f"**Type:** pattern\n"
        merged_content += f"**Topic:** {merged_topic}\n"
        merged_content += f"**Tags:** {merged_tags}\n\n"
        merged_content += f"*Merged from {len(contents)} learnings on {date_str}*\n\n"
        merged_content += "---\n\n"

        for i, content in enumerate(contents):
            source = os.path.basename(file_paths[i]) if file_paths[i] else f"Document {i+1}"
            merged_content += f"## Source: {source}\n\n"
            merged_content += content.strip() + "\n\n"
            merged_content += "---\n\n"

        # Dry run: return what would happen without executing
        if dry_run:
            conn.close()
            return {
                'status': 'dry_run',
                'would_create': merged_path,
                'would_delete': file_paths,
                'source_count': len(contents),
                'merged_scope': merged_scope
            }

        with open(merged_path, 'w', encoding='utf-8') as f:
            f.write(merged_content)

        backed_up = []
        deleted_ids = []
        for i, doc_id in enumerate(docs['ids']):
            file_path = file_paths[i]
            if file_path and os.path.exists(file_path):
                backup = create_backup(file_path, archive_dir)
                if backup:
                    backed_up.append({'original': file_path, 'backup': backup})
                os.remove(file_path)
            db.delete_document(conn, doc_id)
            deleted_ids.append(doc_id)

        merged_id = hashlib.md5(merged_path.encode()).hexdigest()
        merged_metadata = {
            'scope': merged_scope,
            'repo': metadatas[0].get('repo', '') if merged_scope == 'repo' else '',
            'file_path': merged_path,
            'topic': merged_topic,
            'keywords': merged_tags,
        }

        db.upsert_document(conn, merged_id, merged_content, merged_metadata)
        conn.close()

        return {
            'status': 'success',
            'merged_path': merged_path,
            'merged_id': merged_id,
            'merged_count': len(ids),
            'backed_up': backed_up,
            'deleted_ids': deleted_ids
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def main():
    parser = argparse.ArgumentParser(description='Execute consolidation actions')
    subparsers = parser.add_subparsers(dest='action', required=True)

    get_parser = subparsers.add_parser('get', help='Fetch full content for IDs')
    get_parser.add_argument('--ids', required=True, help='Comma-separated document IDs')

    delete_parser = subparsers.add_parser('delete', help='Delete learnings with backup')
    delete_parser.add_argument('--ids', required=True, help='Comma-separated document IDs')

    archive_parser = subparsers.add_parser('archive', help='Move learnings to archive')
    archive_parser.add_argument('--ids', required=True, help='Comma-separated document IDs')

    rescope_parser = subparsers.add_parser('rescope', help='Change scope of a learning')
    rescope_parser.add_argument('--id', required=True, help='Document ID')
    rescope_parser.add_argument('--scope', required=True, choices=['global'], help='New scope')

    merge_parser = subparsers.add_parser('merge', help='Merge multiple learnings')
    merge_parser.add_argument('--ids', required=True, help='Comma-separated document IDs')
    merge_parser.add_argument('--name', required=True, help='Name for merged file (kebab-case)')
    merge_parser.add_argument('--output-dir', help='Override output directory (bypasses scope logic)')
    merge_parser.add_argument('--dry-run', action='store_true', help='Show what would happen without executing')

    args = parser.parse_args()
    config = db.load_config()

    if args.action == 'get':
        ids = [i.strip() for i in args.ids.split(',')]
        result = action_get(ids, config)
    elif args.action == 'delete':
        ids = [i.strip() for i in args.ids.split(',')]
        result = action_delete(ids, config)
    elif args.action == 'archive':
        ids = [i.strip() for i in args.ids.split(',')]
        result = action_archive(ids, config)
    elif args.action == 'rescope':
        result = action_rescope(args.id, args.scope, config)
    elif args.action == 'merge':
        ids = [i.strip() for i in args.ids.split(',')]
        output_dir = getattr(args, 'output_dir', None)
        dry_run = getattr(args, 'dry_run', False)
        result = action_merge(ids, args.name, config, output_dir=output_dir, dry_run=dry_run)
    else:
        result = {'status': 'error', 'message': f'Unknown action: {args.action}'}

    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
