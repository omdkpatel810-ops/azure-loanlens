"""Settings, from environment variables. Nothing secret lives here: in Azure the app signs in to storage
with its managed identity, and locally it talks to the Azurite emulator."""

from __future__ import annotations

import os
from dataclasses import dataclass

AZURITE = "UseDevelopmentStorage=true"  # the Azurite emulator on this computer


@dataclass(frozen=True)
class Settings:
    auth: str = "easyauth"  # easyauth: Azure's built-in sign-in sends who's signed in; dev: a local sign-in page
    storage_account: str = ""  # in Azure: the account name, reached with the managed identity
    storage_connection: str = ""  # locally: a connection string for Azurite
    emulate_events: bool = False  # locally there's no Event Grid, so the app queues the upload event itself
    max_upload_mb: int = 10
    secure_cookies: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        auth = env.get("LOANLENS_AUTH", "easyauth")
        if auth not in ("easyauth", "dev"):
            raise ValueError("LOANLENS_AUTH is easyauth or dev")
        local = auth == "dev"
        return cls(
            auth=auth,
            storage_account=env.get("LOANLENS_STORAGE_ACCOUNT", ""),
            storage_connection=env.get("LOANLENS_STORAGE_CONNECTION", AZURITE if local else ""),
            emulate_events=env.get("LOANLENS_EMULATE_EVENTS", "1" if local else "0") == "1",
            max_upload_mb=int(env.get("LOANLENS_MAX_UPLOAD_MB", "10")),
            secure_cookies=not local,
        )
