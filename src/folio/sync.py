from abc import ABC, abstractmethod
from datetime import datetime, timezone
import asyncio
import logging

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

    async def run_loop(self, interval_seconds: int = 30):
        logger.info(f"Starting Folio SyncEngine (interval: {interval_seconds}s)")
        while True:
            try:
                self.push_pending()
                self.pull_changes()
                self.reconcile()
            except Exception as e:
                logger.error(f"SyncEngine error in loop: {e}")
            await asyncio.sleep(interval_seconds)

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
            return
            
        last_sync = datetime.fromisoformat(state_res.data[0]['last_sync_at'])
        changes = self.adapter.pull_changes(last_sync)
        
        if not changes:
            return
            
        for ext_id, note, edited_at, in_trash in changes:
            if in_trash:
                self.client.table('notes').delete().eq('external_id', ext_id).execute()
            else:
                existing = self.client.table('notes').select('id, path, sync_status').eq('external_id', ext_id).execute()
                
                # Don't overwrite notes with pending local changes
                if existing.data and existing.data[0].get('sync_status') == 'pending_push':
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
                    # Don't overwrite created_at on existing notes
                    data.pop('created_at', None)
                    self.client.table('notes').update(data).eq('external_id', ext_id).execute()
                else:
                    self.client.table('notes').insert(data).execute()
                    
        self.client.table('sync_state').update({
            'last_sync_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', 1).execute()

    def reconcile(self):
        """Run daily to detect items hard-deleted directly on the external platform."""
        state_res = self.client.table('sync_state').select('last_reconciled_at').eq('id', 1).execute()
        if not state_res.data:
            return
            
        last_recon = datetime.fromisoformat(state_res.data[0]['last_reconciled_at'])
        if (datetime.now(timezone.utc) - last_recon).total_seconds() < 86400:
            return # Only reconcile once a day
            
        ext_ids = self.adapter.pull_all_ids()
        active_ext_ids = {ext_id for ext_id, _ in ext_ids}
        
        res = self.client.table('notes').select('path, external_id').not_.is_('external_id', 'null').execute()
        
        for row in res.data:
            if row['external_id'] not in active_ext_ids:
                self.client.table('notes').delete().eq('path', row['path']).execute()
                
        self.client.table('sync_state').update({
            'last_reconciled_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', 1).execute()
