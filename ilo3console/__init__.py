"""Modern, self-contained remote console for HPE iLO 3.

Implements the HP Integrated Remote Console protocol in pure Python:
legacy-TLS login to mint a session token from credentials, the raw TCP
console channel (telnet negotiation + in-protocol RC4/AES encryption), the
proprietary DVC video codec, keyboard/mouse/power input, and virtual media.
A WebSocket bridge exposes it to a browser front-end.

Targets iLO 3 firmware 1.94 on a ProLiant DL380 G7. See docs/PROTOCOL_ILO3.md
for the full protocol documentation.
"""

__version__ = "0.1.0"
