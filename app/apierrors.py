"""Errors the service layer raises; /api/v1 renders them as {"error": {"code", "message", "fields"}}."""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields
