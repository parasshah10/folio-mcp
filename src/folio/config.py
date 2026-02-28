"""Configuration. Loads from environment variables with sensible defaults.
No config file needed — env vars are the standard for Docker/cloud deployment."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class LocalConfig(BaseModel):
    """Local markdown + git backend settings."""

    root: str = Field(
        default_factory=lambda: str(Path.home() / ".folio" / "notes")
    )
    git: bool = True
    git_remote: str | None = None
    git_auto_push: bool = False


class NotionConfig(BaseModel):
    """Notion API backend settings."""

    api_key: str = ""
    database_id: str = ""  # single-database approach


class SupabaseConfig(BaseModel):
    """Supabase API settings."""
    
    url: str = ""
    key: str = ""

class SyncConfig(BaseModel):
    """Background synchronization settings."""
    
    backend: str = "none"
    interval_seconds: int = 10


class SearchConfig(BaseModel):
    """Search behavior settings."""

    max_results: int = 20
    recency_boost: float = 0.3


class FolioConfig(BaseModel):
    """Root configuration. Selects backend and configures each one."""

    backend: str = Field(default="local", description="'local', 'notion', or 'supabase'")
    local: LocalConfig = Field(default_factory=LocalConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    supabase: SupabaseConfig = Field(default_factory=SupabaseConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    warn_tokens: int = 2000

    @classmethod
    def from_env(cls) -> "FolioConfig":
        """Build config from environment variables.

        Environment variables:
            FOLIO_BACKEND          - 'local' or 'notion' (default: local)
            FOLIO_LOCAL_ROOT       - path to notes directory (default: ~/.folio/notes)
            FOLIO_LOCAL_GIT        - enable git versioning (default: true)
            FOLIO_LOCAL_GIT_REMOTE - git remote URL (default: none)
            FOLIO_LOCAL_GIT_PUSH   - auto-push after commit (default: false)
            NOTION_API_KEY         - Notion integration API key
            NOTION_DATABASE_ID     - Notion database ID for all notes
            FOLIO_MAX_RESULTS      - search max results (default: 20)
            FOLIO_RECENCY_BOOST    - search recency weight 0-1 (default: 0.3)
            FOLIO_WARN_TOKENS      - token count warning threshold (default: 2000)
            SUPABASE_URL           - Supabase project URL (for supabase backend)
            SUPABASE_KEY           - Supabase secret/service_role key (for supabase backend)
            FOLIO_SYNC             - sync adapter: 'none' or 'notion' (default: none)
            FOLIO_SYNC_INTERVAL    - sync interval in seconds (default: 30)
        """
        return cls(
            backend=os.environ.get("FOLIO_BACKEND", "local"),
            local=LocalConfig(
                root=os.environ.get(
                    "FOLIO_LOCAL_ROOT",
                    str(Path.home() / ".folio" / "notes"),
                ),
                git=os.environ.get("FOLIO_LOCAL_GIT", "true").lower() == "true",
                git_remote=os.environ.get("FOLIO_LOCAL_GIT_REMOTE"),
                git_auto_push=os.environ.get("FOLIO_LOCAL_GIT_PUSH", "false").lower()
                == "true",
            ),
            notion=NotionConfig(
                api_key=os.environ.get("NOTION_API_KEY", ""),
                database_id=os.environ.get("NOTION_DATABASE_ID", ""),
            ),
            supabase=SupabaseConfig(
                url=os.environ.get("SUPABASE_URL", ""),
                key=os.environ.get("SUPABASE_KEY", ""),
            ),
            sync=SyncConfig(
                backend=os.environ.get("FOLIO_SYNC", "none"),
                interval_seconds=int(os.environ.get("FOLIO_SYNC_INTERVAL", "30")),
            ),
            search=SearchConfig(
                max_results=int(os.environ.get("FOLIO_MAX_RESULTS", "20")),
                recency_boost=float(os.environ.get("FOLIO_RECENCY_BOOST", "0.3")),
            ),
            warn_tokens=int(os.environ.get("FOLIO_WARN_TOKENS", "2000")),
        )

    def validate_backend(self) -> list[str]:
        """Check that the selected backend has required config. Returns errors."""
        errors = []
        if self.backend == "notion":
            if not self.notion.api_key:
                errors.append("NOTION_API_KEY is required for Notion backend")
            if not self.notion.database_id:
                errors.append("NOTION_DATABASE_ID is required for Notion backend")
        elif self.backend == "local":
            root = Path(self.local.root).expanduser()
            try:
                root.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"Failed to create notes directory {root}: {e}")
        elif self.backend == "supabase":
            if not self.supabase.url or not self.supabase.key:
                errors.append("SUPABASE_URL and SUPABASE_KEY are required for Supabase backend")
            if self.sync.backend == "notion":
                if not self.notion.api_key or not self.notion.database_id:
                    errors.append("NOTION_API_KEY and NOTION_DATABASE_ID required for Notion sync via Supabase")
        elif self.backend not in ("local", "notion", "supabase"):
            errors.append(f"Unknown backend: {self.backend}. Use 'local', 'notion', or 'supabase'.")
        return errors
