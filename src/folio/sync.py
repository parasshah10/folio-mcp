from abc import ABC, abstractmethod
from datetime import datetime, timezone
import asyncio
import logging
import time

from supabase import Client
from folio.models import Note

logger = logging.getLogger(__name__)

class SyncAdapter(ABC):
    @abstractmethod
    def push_create(self, note: Note, metadata: dict) -> str:
        """Create note on external platform. Returns external_id."""
        ...

    @abstractmethod
    def push_update(self, external_id: str, note: Note, metadata: dict) -> None:
        """Update existing note on external platform."""
        ...

    @abstractmethod
    def push_delete(self, external_id: str) -> None:
        """Delete note from external platform."""
        ...

    @abstractmethod
    def pull_changes(self, since: datetime) -> list[tuple[str, Note, datetime, bool]]:
        """Fetch notes modified since timestamp. Returns (external_id, Note, external_edited_at, in_trash)."""
        ...

    @abstractmethod
    def pull_all_ids(self) -> list[tuple[str, datetime]]:
        """List all (external_id, last_edited) for reconciliation of hard deletes."""
        ...

class SyncEngine:
    def __init__(self, adapter: SyncAdapter, client: Client):
        self.adapter = adapter
        self.client = client

    def run_loop(self, interval_seconds: int = 30):
        logger.info(f"Starting Folio SyncEngine — pull only (interval: {interval_seconds}s)")
        while True:
            try:
                self.pull_changes()
                self.reconcile()
            except Exception as e:
                logger.error(f"SyncEngine error in loop: {e}")
            time.sleep(interval_seconds)

    def push_pending(self):
        """Push local changes to the external platform."""
        res = self.client.table('notes').select('*').in_('sync_status', ['pending_push', 'pending_delete']).execute()
        
        for row in res.data:
            path = row['path']
            try:
                if row['sync_status'] == 'pending_delete':
                    if row.get('external_id'):
                        self.adapter.push_delete(row['external_id'])
                    self.client.table('notes').delete().eq('path', path).execute()
                    
                elif row['sync_status'] == 'pending_push':
                    note = Note(
                        path=row['path'], 
                        content=row['content'], 
                        tags=row['tags'],
                        created=datetime.fromisoformat(row['created_at']),
                        updated=datetime.fromisoformat(row['updated_at']),
                        metadata=row.get('metadata', {})
                    )
                    
                    if not row.get('external_id'):
                        ext_id = self.adapter.push_create(note, row.get('metadata', {}))
                        self.client.table('notes').update({
                            'external_id': ext_id, 
                            'sync_status': 'synced',
                            'last_synced_at': datetime.now(timezone.utc).isoformat()
                        }).eq('path', path).execute()
                    else:
                        self.adapter.push_update(row['external_id'], note, row.get('metadata', {}))
                        self.client.table('notes').update({
                            'sync_status': 'synced',
                            'last_synced_at': datetime.now(timezone.utc).isoformat()
                        }).eq('path', path).execute()
            except Exception as e:
                logger.error(f"Failed to push pending changes for {path}: {e}")

    def pull_changes(self):
        """Pull remote changes from the external platform."""
        state_res = self.client.table('sync_state').select('*').eq('id', 1).execute()
        if not state_res.data:
            logger.warning("SyncEngine: No sync_state found in database.")
            return
            
        last_sync = datetime.fromisoformat(state_res.data[0]['last_sync_at'])
        logger.debug(f"SyncEngine: Pulling changes since {last_sync.isoformat()}")
        
        try:
            changes = self.adapter.pull_changes(last_sync)
        except Exception as e:
            logger.error(f"SyncEngine: Adapter pull_changes failed: {e}")
            return
        
        if not changes:
            logger.debug("SyncEngine: No remote changes found.")
            return
            
        logger.info(f"SyncEngine: Found {len(changes)} remote changes.")
        
        new_last_sync = last_sync
        for ext_id, note, edited_at, in_trash in changes:
            try:
                if edited_at > new_last_sync:
                    new_last_sync = edited_at
                    
                if in_trash:
                    logger.info(f"SyncEngine: Deleting trashed note (ext_id: {ext_id})")
                    self.client.table('notes').delete().eq('external_id', ext_id).execute()
                else:
                    # Match by external_id
                    existing = self.client.table('notes').select('id, path, title, sync_status').eq('external_id', ext_id).execute()
                    
                    derived_path = note.path

                    # -------------------------------------------------------------
                    # Intelligent Auto-Link Path Evaluation
                    # -------------------------------------------------------------
                    current_slug = derived_path.split("/")[-1]
                    from folio.backends.notion import _slugify_title

                    is_untitled = bool(current_slug.startswith("untitled-"))

                    if existing.data:
                        old_path = existing.data[0]['path']
                        old_title = existing.data[0]['title']

                        old_title_slug = _slugify_title(old_title) if old_title and old_title != "Untitled" else None

                        # Did the user manually change the path in Notion without changing the title?
                        manual_path_override = (derived_path != old_path) and (note.title == old_title)

                        # Was the old path directly derived from the old title?
                        was_auto_linked = bool(old_title_slug and old_path.split("/")[-1] == old_title_slug)

                        should_auto_link = (was_auto_linked and not manual_path_override) or is_untitled
                    else:
                        # For brand new notes, if the path is a placeholder but the user has already
                        # typed a real title, we must evaluate it as auto-linked immediately.
                        should_auto_link = is_untitled

                    if should_auto_link:
                        # It is auto-linked! Generate new expected path based on new Notion Title and Folder
                        new_slug = _slugify_title(note.title) if note.title and note.title != "Untitled" else None
                        if not new_slug:
                            new_slug = f"untitled-{ext_id[:8]}.md"
                        derived_path = f"{note.folder}/{new_slug}" if note.folder else new_slug

                        # If we generated a new path, we should ideally push it back to Notion here too.
                        # We can do this silently in the background.
                        if derived_path != note.path:
                            def _push_path_fix(page_id, new_path):
                                try:
                                    self.adapter._backend.client.pages.update(
                                        page_id=page_id,
                                        properties={"folio_path": {"rich_text": [{"text": {"content": new_path}}]}}
                                    )
                                    self.adapter._backend._cache[new_path] = page_id
                                except Exception:
                                    pass
                            import threading
                            threading.Thread(target=_push_path_fix, args=(ext_id, derived_path), daemon=True).start()
                    else:
                        # It's an EXPLICIT path. Leave the custom filename alone!
                        # But we DO want to sync folder moves if the Notion folder property changed.
                        explicit_slug = current_slug
                        derived_path = f"{note.folder}/{explicit_slug}" if note.folder else explicit_slug

                        if derived_path != note.path:
                            def _push_folder_fix(page_id, new_path):
                                try:
                                    self.adapter._backend.client.pages.update(
                                        page_id=page_id,
                                        properties={"folio_path": {"rich_text": [{"text": {"content": new_path}}]}}
                                    )
                                    self.adapter._backend._cache[new_path] = page_id
                                except Exception:
                                    pass
                            import threading
                            threading.Thread(target=_push_folder_fix, args=(ext_id, derived_path), daemon=True).start()

                    if not existing.data:
                        # Fallback: Match by path (to link existing unlinked rows)
                        logger.debug(f"SyncEngine: No external_id match for {derived_path}, checking by path.")
                        existing = self.client.table('notes').select('id, path, title, sync_status').eq('path', derived_path).execute()

                    # Don't overwrite notes with pending local changes
                    if existing.data:
                        current_status = existing.data[0].get('sync_status')
                        if current_status in ('pending_push', 'pending_delete'):
                            logger.debug(f"SyncEngine: Skipping pull for {derived_path} due to pending local state ({current_status}).")
                            continue
                    
                    data = {
                        'path': derived_path,
                        'title': note.title,
                        'content': note.content,
                        'tags': note.tags,
                        'folder': note.folder,
                        'size_tokens': note.size_tokens,
                        'external_id': ext_id,
                        'external_edited_at': edited_at.isoformat(),
                        'sync_status': 'synced',
                        'created_at': note.created.isoformat(),
                        'updated_at': note.updated.isoformat(),
                        'last_synced_at': datetime.now(timezone.utc).isoformat()
                    }
                    if existing.data:
                        logger.info(f"SyncEngine: Updating/Linking note from Notion: {derived_path}")

                        # Path collision prevention for renamed notes
                        if existing.data[0]['path'] != derived_path:
                            existing_path = self.client.table('notes').select('id').eq('path', derived_path).execute()
                            if existing_path.data and existing_path.data[0]['id'] != existing.data[0]['id']:
                                # Append short ID suffix to avoid collision
                                base = derived_path.rsplit('.md', 1)[0]
                                derived_path = f"{base}-{ext_id[:8]}.md"
                                data['path'] = derived_path
                                logger.info(f"SyncEngine: Update path collision avoided, using {derived_path}")

                        # Don't overwrite created_at on existing notes
                        data.pop('created_at', None)
                        # Use id or path to update to ensure we target the right row
                        self.client.table('notes').update(data).eq('id', existing.data[0]['id']).execute()
                    else:
                        # Path collision prevention for new notes
                        existing_path = self.client.table('notes').select('id').eq('path', derived_path).execute()
                        if existing_path.data:
                            # Append short ID suffix to avoid collision
                            base = derived_path.rsplit('.md', 1)[0]
                            derived_path = f"{base}-{ext_id[:8]}.md"
                            data['path'] = derived_path
                            logger.info(f"SyncEngine: Insert path collision avoided, using {derived_path}")

                        logger.info(f"SyncEngine: Inserting new note from Notion: {derived_path}")
                        self.client.table('notes').insert(data).execute()
            except Exception as e:
                logger.error(f"SyncEngine: Failed to process change for {ext_id}: {e}")
                # Continue to next change so one error doesn't block the whole sync
        
        # Avoid getting stuck if we process changes but timestamps are equal
        if new_last_sync == last_sync and changes:
             # Add 1ms to move past the current window
             from datetime import timedelta
             new_last_sync = last_sync + timedelta(milliseconds=1)
                    
        self.client.table('sync_state').update({
            'last_sync_at': new_last_sync.isoformat()
        }).eq('id', 1).execute()

    def reconcile(self):
        """Run frequently to detect items hard-deleted directly on the external platform."""
        state_res = self.client.table('sync_state').select('last_reconciled_at').eq('id', 1).execute()
        if not state_res.data:
            return
            
        last_recon = datetime.fromisoformat(state_res.data[0]['last_reconciled_at'])
        if (datetime.now(timezone.utc) - last_recon).total_seconds() < 120:
            return # Only reconcile every 120 seconds (2 minutes)
            
        logger.info("SyncEngine: Running full reconciliation...")
        try:
            ext_ids = self.adapter.pull_all_ids()
        except Exception as e:
            logger.error(f"SyncEngine: Adapter pull_all_ids failed: {e}")
            return
            
        active_ext_ids = {ext_id for ext_id, _ in ext_ids}
        logger.debug(f"SyncEngine: {len(active_ext_ids)} active IDs found on remote.")
        
        res = self.client.table('notes').select('path, external_id').not_.is_('external_id', 'null').execute()
        
        deleted_count = 0
        for row in res.data:
            if row['external_id'] not in active_ext_ids:
                logger.info(f"SyncEngine: Reconciled deletion of {row['path']} (Notion page {row['external_id']} no longer active)")
                self.client.table('notes').delete().eq('path', row['path']).execute()
                deleted_count += 1
                
        self.client.table('sync_state').update({
            'last_reconciled_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', 1).execute()
        logger.info(f"SyncEngine: Reconciliation complete. Deleted {deleted_count} notes.")
