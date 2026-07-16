"""Modern, self-contained remote console for HPE iLO 100 (ServerEngines "Mahogany").

Implements the Mahogany KVM protocol in pure Python: a VNC-derived binary
protocol carried over a single TCP or SSL socket for combined
video/keyboard/mouse, plus a separate storage channel for virtual media.

See docs/PROTOCOL_ILO100.md for the full protocol documentation.
"""

__version__ = "0.1.0"
