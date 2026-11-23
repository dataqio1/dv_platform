"""Provider catalog — the extensible source of truth for what each channel needs.

Drives the Admin form (data-driven: add a provider here and the UI adapts) and tells the
service which fields are secret (→ SecretStore) vs config (→ DB). "etc" = add an entry.
"""

from __future__ import annotations

SEVERITY_ORDER = ("info", "warning", "critical")
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}


def severity_rank(sev: str | None) -> int:
    return _SEVERITY_RANK.get((sev or "warning").lower(), 1)


# Each field: {key, label, input: text|number|password, secret: bool}
PROVIDER_SPECS: list[dict] = [
    {
        "provider": "email",
        "category": "channel",
        "label": "Email (SMTP)",
        "fields": [
            {"key": "to", "label": "To address", "input": "text", "secret": False},
            {"key": "from", "label": "From address", "input": "text", "secret": False},
            {"key": "smtp_host", "label": "SMTP host", "input": "text", "secret": False},
            {"key": "smtp_port", "label": "SMTP port", "input": "number", "secret": False},
            {"key": "username", "label": "SMTP username", "input": "text", "secret": True},
            {"key": "password", "label": "SMTP password", "input": "password", "secret": True},
        ],
    },
    {
        "provider": "slack",
        "category": "channel",
        "label": "Slack",
        "fields": [
            {"key": "webhook_url", "label": "Incoming webhook URL", "input": "password", "secret": True},
        ],
    },
    {
        "provider": "teams",
        "category": "channel",
        "label": "Microsoft Teams",
        "fields": [
            {"key": "webhook_url", "label": "Incoming webhook URL", "input": "password", "secret": True},
        ],
    },
    {
        "provider": "webhook",
        "category": "channel",
        "label": "Webhook",
        "fields": [
            {"key": "url", "label": "POST URL", "input": "password", "secret": True},
        ],
    },
    {
        "provider": "pagerduty",
        "category": "channel",
        "label": "PagerDuty",
        "fields": [
            {"key": "routing_key", "label": "Integration key", "input": "password", "secret": True},
        ],
    },
]

_BY_PROVIDER = {p["provider"]: p for p in PROVIDER_SPECS}


def known_provider(provider: str) -> bool:
    return provider in _BY_PROVIDER


def secret_field_keys(provider: str) -> set[str]:
    spec = _BY_PROVIDER.get(provider)
    return {f["key"] for f in spec["fields"] if f["secret"]} if spec else set()


def provider_specs() -> dict:
    return {
        "providers": PROVIDER_SPECS,
        "events": ["incident_opened"],
        "severities": list(SEVERITY_ORDER),
    }
