import sys
from folio.config import FolioConfig
from folio.backends.notion import NotionBackend
from supabase import create_client

def migrate_notion_to_supabase():
    config = FolioConfig.from_env()
    
    if not config.notion.api_key or not config.notion.database_id:
        print("Error: NOTION_API_KEY and NOTION_DATABASE_ID must be set")
        sys.exit(1)
        
    if not config.supabase.url or not config.supabase.key:
        print("Error: SUPABASE_URL and SUPABASE_KEY must be set")
        sys.exit(1)
        
    print("Loading Notion backend...")
    notion_backend = NotionBackend(config.notion)
    
    print("Exporting all notes from Notion (this may take a while)...")
    notes = notion_backend.export_all()
    print(f"Exported {len(notes)} notes.")
    
    print("Connecting to Supabase...")
    supabase = create_client(config.supabase.url, config.supabase.key)
    
    print("Pushing to Supabase Cache...")
    for note in notes:
        # Resolve the Notion page_id from the internal cache
        page_id = notion_backend._cache.get(note.path)
        
        data = {
            "path": note.path,
            "title": note.title,
            "content": note.content,
            "tags": note.tags,
            "folder": note.folder,
            "size_tokens": note.size_tokens,
            "created_at": note.created.isoformat(),
            "updated_at": note.updated.isoformat(),
            "external_id": page_id,
            "external_edited_at": note.updated.isoformat(),
            "sync_status": "synced",
            "metadata": note.metadata
        }
        
        supabase.table("notes").upsert(data, on_conflict="path").execute()
        print(f"Migrated: {note.path}")
        
    print("Migration complete!")

if __name__ == "__main__":
    migrate_notion_to_supabase()
