class IdeasyncError(RuntimeError):
    """An expected, user-actionable ideasync failure."""


class ConfigError(IdeasyncError):
    """The local ideasync configuration is absent or invalid."""


class ContractError(IdeasyncError):
    """An ideas-tier file does not meet the required contract."""


class GitError(IdeasyncError):
    """A managed Git operation failed safely."""


class LockBusy(IdeasyncError):
    """Another sync owns the repository lock."""
