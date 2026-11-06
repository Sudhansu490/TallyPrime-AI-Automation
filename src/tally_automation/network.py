"""Network ranges shared by the server (bind addresses) and the TallyPrime client."""

import ipaddress

# Addresses Tailscale assigns to tailnet devices.
TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
