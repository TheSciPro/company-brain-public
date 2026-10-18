"""Domain exceptions. Never swallow; fail closed on access."""


class NwbError(Exception):
    """Base error."""


class MigrationError(NwbError):
    """A migration failed or the migration history is inconsistent."""


class DataLoadError(NwbError):
    """Source data is malformed or violates an invariant."""


class ConfigError(NwbError):
    """Configuration is missing or invalid (e.g. an unknown provider or a missing API key)."""


class AccessDeniedError(NwbError):
    """The caller may not read or change this resource. Messages never reveal the content."""


class NotFoundError(NwbError):
    """The resource does not exist (or is hidden from the caller, which looks the same)."""


class PromptError(NwbError):
    """A prompt file is missing, malformed, or rendered with missing variables."""


class ConflictError(NwbError):
    """The request conflicts with current state (e.g. a turn on an ended session)."""
