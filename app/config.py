from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./academy.db"
    secret_key: str = "change-this-long-random-secret"
    access_token_expire_minutes: int = 480
    admin_name: str = "Administradora"
    admin_email: str = "admin@chemistry-academy.com"
    admin_password: str = "ChangeMe123!"
    storage_dir: Path = Path("./storage")
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
