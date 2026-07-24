from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from .config import settings


class LocalStorage:
    def __init__(self) -> None:
        self.root = settings.storage_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, upload: UploadFile) -> tuple[str, int]:
        suffix = Path(upload.filename or "").suffix.lower()
        key = f"{uuid4().hex}{suffix}"
        destination = self.root / key
        size = 0
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                output.write(chunk)
        return key, size

    def path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate.parent != self.root:
            raise ValueError("Ruta de almacenamiento no válida")
        return candidate


storage = LocalStorage()

