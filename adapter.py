"""Hermes plugin entry shim for the Telex platform.

The repository root is the Hermes plugin directory. Hermes imports this root
``adapter.py`` shim; the implementation lives in the ``hermes_telex`` package.
"""

try:
    from .hermes_telex.adapter import (  # noqa: F401
        TELEX_PLATFORM,
        TELEX_PLUGIN_NAME,
        TelexAdapter,
        check_telex_requirements,
        register,
    )
except ImportError:  # pragma: no cover - direct local import fallback.
    from hermes_telex.adapter import (  # noqa: F401
        TELEX_PLATFORM,
        TELEX_PLUGIN_NAME,
        TelexAdapter,
        check_telex_requirements,
        register,
    )
