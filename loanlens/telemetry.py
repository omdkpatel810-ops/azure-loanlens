"""Sending traces, requests and logs to Application Insights, when LoanLens runs in Azure.

Application Insights has local authentication turned off, so even its connection string can't be used to
send data without an Entra ID token: each app signs in with its own managed identity. On a computer
without APPLICATIONINSIGHTS_CONNECTION_STRING, this does nothing.
"""

from __future__ import annotations

import os


def setup(role: str, env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ if env is None else env)
    connection = env.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection:
        return False
    os.environ.setdefault("OTEL_SERVICE_NAME", f"loanlens-{role}")  # pragma: no cover - only in Azure
    from azure.identity import ManagedIdentityCredential  # pragma: no cover
    from azure.monitor.opentelemetry import configure_azure_monitor  # pragma: no cover

    configure_azure_monitor(  # pragma: no cover
        connection_string=connection, credential=ManagedIdentityCredential(client_id=env.get("AZURE_CLIENT_ID"))
    )
    return True  # pragma: no cover
