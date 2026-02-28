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
                    existing = self.client.table('notes').select('id, path, sync_status').eq('external_id', ext_id).execute()
                    
                    if not existing.data:
                        # Fallback: Match by path (to link existing unlinked rows)
                        logger.debug(f"SyncEngine: No external_id match for {note.path}, checking by path.")
                        existing = self.client.table('notes').select('id, path, sync_status').eq('path', note.path).execute()

                    # Don't overwrite notes with pending local changes
                    if existing.data and existing.data[0].get('sync_status') == 'pending_push':
                        logger.debug(f"SyncEngine: Skipping pull for {note.path} due to pending local changes.")
                        continue
                    
                    data = {
                        'path': note.path,
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
                        logger.info(f"SyncEngine: Updating/Linking note from Notion: {note.path}")
                        # Don't overwrite created_at on existing notes
                        data.pop('created_at', None)
                        # Use id or path to update to ensure we target the right row
                        self.client.table('notes').update(data).eq('id', existing.data[0]['id']).execute()
                    else:
                        logger.info(f"SyncEngine: Inserting new note from Notion: {note.path}")
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
        """Run daily to detect items hard-deleted directly on the external platform."""
        state_res = self.client.table('sync_state').select('last_reconciled_at').eq('id', 1).execute()
        if not state_res.data:
            return
            
        last_recon = datetime.fromisoformat(state_res.data[0]['last_reconciled_at'])
        if (datetime.now(timezone.utc) - last_recon).total_seconds() < 86400:
            return # Only reconcile once a day
            
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
                logger.info(f"SyncEngine: Reconcile deleting {row['path']} (not on remote)")
                self.client.table('notes').delete().eq('path', row['path']).execute()
                deleted_count += 1
                
        self.client.table('sync_state').update({
            'last_reconciled_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', 1).execute()
        logger.info(f"SyncEngine: Reconciliation complete. Deleted {deleted_count} notes.")
