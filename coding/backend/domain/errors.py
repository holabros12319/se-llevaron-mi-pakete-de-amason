class ValidationError(ValueError):
    """Input or business rule violation safe to show to the user."""


class NotFoundError(LookupError):
    """Requested product does not exist."""


class InsufficientStockError(ValidationError):
    """Requested quantity exceeds available stock."""


class EmptyCartError(ValidationError):
    """A sale cannot be completed without any cart lines."""
