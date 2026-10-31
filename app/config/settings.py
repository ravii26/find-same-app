# app/config/settings.py
"""Central settings loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

# Project root = directory containing this package (test_new/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    api_key: str = "change-me-in-production"
    allow_insecure_defaults: bool = False

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 20
    redis_status_ttl: int = 604800  # 7 days

    # Storage — default to inside the project tree for local dev
    data_dir: Path = _PROJECT_ROOT / "data"
    uploads_dir: Path = _PROJECT_ROOT / "uploads"

    # Queue / Concurrency
    max_concurrent_scans: int = 4
    max_queue_depth: int = 100
    # Run a Dramatiq worker inside the API process. Off in docker-compose, where the worker container consumes the
    # queues: two consumers on one queue race for the same scans.
    embedded_worker: bool = True

    # Storefronts every scan searches (comma-separated ISO codes). Scans are not filtered by country.
    # If DEFAULT_COUNTRIES is set in the environment, that value wins over this default.
    default_countries: str = "US,IN,GB,SG,AE"

    # Scan Limits
    max_keywords: int = 60
    max_response_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_apk_bytes: int = 100 * 1024 * 1024  # 100 MB
    scan_timeout_seconds: int = 600

    # HTTP Client
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 30.0
    http_max_redirects: int = 10

    # API Keys (optional)
    github_token: str = ""
    gitlab_token: str = ""
    virustotal_api_key: str = ""
    otx_api_key: str = ""
    malwarebazaar_api_key: str = ""
    urlhaus_api_key: str = ""

    # Evidence capture
    evidence_max_hits: int = 25
    evidence_concurrency: int = 3

    # Elasticsearch — results are always indexed after each scan (output.json stays authoritative)
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_api_key: str = ""
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    elasticsearch_verify_certs: bool = True
    elasticsearch_index_prefix: str = "rogue"
    elasticsearch_timeout: int = 30
    elasticsearch_shards: int = 1
    elasticsearch_replicas: int = 1

    # Webhook
    webhook_timeout: int = 10

    # APK Analysis
    analyze_apks: bool = False
    capture_evidence: bool = False
    apk_analysis_cpu_seconds: int = 30
    apk_analysis_memory_mb: int = 256
    apk_analysis_wall_seconds: int = 60
    apk_phase_seconds: int = 180  # total time budget for the APK analysis pass; partial results are kept
    apk_discovery_pages: int = 30  # most web pages opened per scan to look for an offered APK
    apk_discovery_seconds: int = 60  # time budget for that step
    apk_download_seconds: int = 90  # wall-clock cap on one APK download; a slow server cannot hold the pass

    def scan_ceiling_seconds(self) -> int:
        """Hard limit for one scan. The APK analysis and discovery passes have their own budgets, so they are
        added on top of the normal pipeline limit instead of being squeezed into it."""
        return self.scan_timeout_seconds + self.apk_phase_seconds + self.apk_discovery_seconds

    @field_validator("data_dir", "uploads_dir", mode="before")
    @classmethod
    def coerce_path(cls, v: str | Path) -> Path:
        return Path(v)

    def resolve_upload_path(self, name: str) -> Path:
        """Resolve a caller-supplied path against UPLOADS_DIR; ValueError if it escapes."""
        root = self.uploads_dir.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("apk_path must be inside UPLOADS_DIR")
        return path

    def default_country_list(self) -> list[str]:
        codes = [c.strip().upper() for c in self.default_countries.split(",") if c.strip()]
        return codes or ["US"]

    def results_dir(self) -> Path:
        return self.data_dir / "results"

    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def api_lock_path(self) -> Path:
        return self.data_dir / ".api.lock"

    def scan_result_dir(self, scan_id: str) -> Path:
        return self.results_dir() / scan_id

    def scan_output_path(self, scan_id: str) -> Path:
        return self.scan_result_dir(scan_id) / "output.json"

    def scan_log_path(self, scan_id: str) -> Path:
        return self.scan_result_dir(scan_id) / "scan.log"

_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings