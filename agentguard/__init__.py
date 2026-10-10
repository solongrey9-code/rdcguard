"""AgentGuard - boot autostart and self-healing watchdog for local agent/MCP services.

Born from two production mechanisms on a Windows machine: the Desktop Commander
remote-device watchdog (PowerShell) and the isolated OpenCode V2 + OpenChamber
native host (Python). AgentGuard generalizes both into a declarative, fail-closed
guard framework that can protect anything: MCP servers, local bridges, proxies,
model servers, background daemons.
"""

__version__ = "1.0.0"
