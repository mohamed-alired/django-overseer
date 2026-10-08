class OverseerError(Exception):
    """Base class for Overseer errors."""


class RunAbandoned(OverseerError):
    """Recorded on an attempt that exceeded its timeout or lost its worker (never raised)."""


class AdapterUnsupported(OverseerError):
    """The task backend does not support this operation."""


class RunLost(OverseerError):
    """The backend no longer has the task behind a waiting run; it will never execute."""
