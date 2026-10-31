"""Host-independent optimal ordering engine."""
import sys

if sys.platform=='win32':
    from coupon_split.windows_mcp import install
    install()
