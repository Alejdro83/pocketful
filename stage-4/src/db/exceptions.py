"""Custom exceptions for Pocketful wallet operations."""


class InsufficientFunds(Exception):
    """Raised when a transfer exceeds available balance."""

    def __init__(self, user_id: str, amount: int, available: int):
        self.user_id = user_id
        self.amount = amount
        self.available = available
        super().__init__(
            f"User {user_id} has {available} but {amount} required"
        )


class DuplicateTransaction(Exception):
    """Raised when an idempotency_key collision is detected outside the happy path."""

    def __init__(self, idempotency_key: str):
        self.idempotency_key = idempotency_key
        super().__init__(f"Duplicate transaction with key: {idempotency_key}")


class IdempotencyKeyReuse(Exception):
    """Same idempotency_key but different request body — 409 conflict."""

    def __init__(self, user_id: str, idempotency_key: str):
        self.user_id = user_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Idempotency key {idempotency_key} already used with different payload for user {user_id}"
        )


class SelfPayment(Exception):
    """Raised when a user tries to send money to themselves."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"Cannot send payment to self: {user_id}")