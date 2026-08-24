"""Application error that carries a user-facing Swahili message.
Raised by validation/commit code, converted to JSON by the handler in main."""


class AppError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class DuplicateCommit(Exception):
    """Raised internally when an idempotency key was already used; the caller
    returns the original receipt instead of executing again."""

    def __init__(self, receipt: dict):
        super().__init__("duplicate commit")
        self.receipt = receipt
