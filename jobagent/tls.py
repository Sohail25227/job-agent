"""TLS trust setup.

On a corporate network (Amdocs included) HTTPS is intercepted by a proxy that
re-signs traffic with an internal root CA. That CA is installed in the macOS
keychain, but Python's bundled ``certifi`` store knows nothing about it, so
every request fails with CERTIFICATE_VERIFY_FAILED.

``truststore`` makes Python verify against the OS trust store instead, which
fixes it without ever disabling verification.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_applied = False


def enable_system_trust(ca_bundle: str | None = None) -> bool:
    """Verify TLS against the OS trust store (and an extra CA bundle if given)."""
    global _applied
    if _applied:
        return True

    if ca_bundle:
        path = Path(ca_bundle).expanduser()
        if path.is_file():
            os.environ.setdefault("SSL_CERT_FILE", str(path))
            os.environ.setdefault("REQUESTS_CA_BUNDLE", str(path))
            log.debug("using CA bundle %s", path)
        else:
            log.warning("http.ca_bundle %s does not exist; ignoring", path)

    try:
        import truststore

        truststore.inject_into_ssl()
        _applied = True
        log.debug("verifying TLS against the system trust store")
        return True
    except ImportError:
        log.debug("truststore not installed; using certifi defaults")
    except Exception as exc:
        log.warning("could not enable system trust store: %s", exc)
    return False
