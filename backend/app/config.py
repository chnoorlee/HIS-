from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HIS_", env_file=".env", extra="ignore")
    env: str = "development"
    database_url: str = "sqlite:///./data/his.db"
    data_dir: Path = Path("./data")
    jwt_secret: str = ""
    audio_key: str = ""
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174"
    run_worker: bool = True
    model_provider: str = "demo"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    asr_provider: str = "unavailable"
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""
    approved_model_hosts: str = ""
    emr_provider: str = "mock"
    emr_base_url: str = ""
    emr_api_key: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_client_id: str = ""
    oidc_scope: str = "openid profile"
    audio_retention_hours: int = 24
    content_retention_days: int = 30
    job_lease_seconds: int = 60
    max_audio_chunk_bytes: int = 320000
    source_max_age_seconds: int = 86400

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.env not in {"development", "test", "production"}:
            raise RuntimeError("Invalid HIS_ENV")
        if self.env == "production":
            required = [self.jwt_secret, self.audio_key, self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url, self.oidc_client_id]
            if not all(required) or len(self.jwt_secret) < 32:
                raise RuntimeError("Production requires persistent secrets and an OIDC verifier")
            if self.database_url.startswith("sqlite") or self.model_provider == "demo" or self.emr_provider == "mock":
                raise RuntimeError("Production forbids SQLite and demonstration providers")
            if self.emr_provider != "http" or not self.emr_api_key:
                raise RuntimeError("Production requires an authenticated HTTPS EMR connector")
            for endpoint in [self.emr_base_url, self.oidc_issuer, self.oidc_jwks_url]:
                parsed = urlparse(endpoint)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise RuntimeError("Production service endpoints require valid HTTPS URLs")
            for origin in self.allowed_origins.split(","):
                parsed = urlparse(origin.strip())
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                    raise RuntimeError("Production browser origins require explicit HTTPS origins")
        if not self.jwt_secret:
            self.jwt_secret = self._local_secret("jwt.key", lambda: __import__("secrets").token_urlsafe(48))
        if not self.audio_key:
            self.audio_key = self._local_secret("audio.key", lambda: Fernet.generate_key().decode())
        Fernet(self.audio_key.encode())
        for provider, url in [(self.model_provider, self.llm_base_url), (self.asr_provider, self.asr_base_url)]:
            if provider == "openai_compatible":
                self.validate_model_url(url)
        for provider, credential, model in [(self.model_provider, self.llm_api_key, self.llm_model), (self.asr_provider, self.asr_api_key, self.asr_model)]:
            if provider == "openai_compatible" and (not credential or not model):
                raise RuntimeError("Model providers require a credential and explicit model")
        if self.model_provider not in {"demo", "openai_compatible", "unavailable"} or self.asr_provider not in {"unavailable", "openai_compatible", "dashscope_realtime"} or self.emr_provider not in {"mock", "http"}:
            raise RuntimeError("Unsupported provider configuration")
        if self.asr_provider == "dashscope_realtime":
            parsed = urlparse(self.asr_base_url)
            allowed = {host.strip() for host in self.approved_model_hosts.split(",") if host.strip()}
            if parsed.scheme != "wss" or not parsed.hostname or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.fragment or not self.asr_api_key or not self.asr_model:
                raise RuntimeError("Realtime ASR requires an approved WSS endpoint, credential and explicit model")

    def _local_secret(self, name, generator):
        path = self.data_dir / name
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write(generator())
            path.chmod(0o600)
        except FileExistsError:
            pass
        return path.read_text(encoding="ascii")

    def validate_model_url(self, url: str) -> None:
        parsed = urlparse(url)
        allowed = {host.strip() for host in self.approved_model_hosts.split(",") if host.strip()}
        if not parsed.hostname or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise RuntimeError("Model endpoint is not on HIS_APPROVED_MODEL_HOSTS")
        if parsed.scheme != "https" and not (self.env != "production" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise RuntimeError("Approved model endpoints require HTTPS")


settings = Settings()
