"""Typed API errors. Handlers raise these; the server turns them into JSON."""


class ApiError(Exception):
    """An error with an HTTP status code and a machine-readable code.

    Raised anywhere in the request path; the server catches it and renders
    a JSON body of the shape ``{"error": {"code": ..., "message": ...}}``.
    """

    def __init__(self, status, code, message, **extra):
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)

    def to_dict(self):
        body = {"code": self.code, "message": self.message}
        body.update(self.extra)
        return {"error": body}


# Convenience constructors for the common cases ---------------------------------


def bad_request(message, **extra):
    return ApiError(400, "bad_request", message, **extra)


def not_found(message, **extra):
    return ApiError(404, "not_found", message, **extra)


def conflict(message, **extra):
    return ApiError(409, "conflict", message, **extra)
