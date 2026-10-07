class OverseerError(Exception):
    """Base class for Overseer errors."""


class RunAbandoned(OverseerError):
    """Recorded on an attempt that exceeded its timeout or lost its worker (never raised)."""


class AdapterUnsupported(OverseerError):
    """The task backend does not support this operation."""
