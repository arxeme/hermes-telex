try:
    from .adapter import register
except ImportError:  # pragma: no cover
    register = None  # type: ignore

__all__ = ["register"]
