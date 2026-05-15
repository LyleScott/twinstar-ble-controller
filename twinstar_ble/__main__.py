"""Entry point for ``python -m twinstar_ble``.

Delegates to :func:`twinstar_ble.main` so the module-form invocation matches
what the ``twinstar-ble`` console script does.
"""

from . import main

if __name__ == "__main__":
    main()
