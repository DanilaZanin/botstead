"""Ошибки лаунчера. У каждой есть HTTP-статус и код, который уходит клиенту в `{"error": {...}}`."""


class LauncherError(Exception):
    status = 500
    code = "error"


class ValidationFailed(LauncherError, ValueError):
    status = 400
    code = "invalid"


class Unauthorized(LauncherError):
    status = 401
    code = "unauthorized"


class NotManaged(LauncherError):
    """Объект существует, но без метки лаунчера (или чужой роли): трогать нельзя."""
    status = 403
    code = "not_managed"


class NotFound(LauncherError):
    status = 404
    code = "not_found"


class Conflict(LauncherError):
    status = 409
    code = "conflict"


class Frozen(Conflict):
    code = "frozen"


class Busy(LauncherError):
    status = 429
    code = "busy"


class BackendError(LauncherError):
    status = 502
    code = "docker_error"


class StateError(LauncherError):
    """Лаунчер не смог записать или прочитать собственное состояние (метки заморозки и т.п.)."""
    status = 503
    code = "state_error"


class NetPolicyError(LauncherError):
    status = 503
    code = "netpolicy"
