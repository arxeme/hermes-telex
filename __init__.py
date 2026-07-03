try:
    from .hermes_telex.adapter import register
except ImportError:  # pragma: no cover - direct local import fallback.
    from hermes_telex.adapter import register

__all__ = ["register"]
