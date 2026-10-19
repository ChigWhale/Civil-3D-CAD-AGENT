"""What a prompt-block run needs that costs something to build: the tool
registry (importing cad_mcp_server, about a second), the model backends
and the Executor that calls the MCP server's own functions in process.

Built in a worker thread by the dashboard server, never on its event loop:
the tool functions resolve the dashboard's drawing and report their
activity over HTTP to that very server, so a call made on the loop would
wait on itself.

Order matters at the end: Executor._point_servers aims
dashboard_events.DASHBOARD_BASE at the redline settings' dashboard, so the
server's own address is written after the Executor exists. The drafting
tools hold the file resolver as a closure, which is why the module global
is set rather than a function patched.
"""
from dataclasses import dataclass

from redline import config, tools
from redline.act import Executor

from . import backends


@dataclass
class Session:
    settings: object
    sequencer: object
    filler: object
    executor: Executor
    model: str
    tools_version: str


def open(dashboard_id: str, file: str, on_conflict: str = "pause", self_base: str | None = None,
         plugin_base: str | None = None, log=None, fake_filler_rules=None) -> Session:
    settings = config.load()
    tools.registry()
    sequencer, filler = backends.backends(settings, fake_filler_rules)
    # warn: the Executor acknowledges and repeats on its own; anything else
    # comes back blocked for the dashboard to pause on or stop at.
    ex = Executor(settings, mode="run", on_conflict=("warn" if str(on_conflict).lower() == "warn" else "pause"),
                  backend=filler, log=log)
    ex.ack_reason = "prompt block, policy warn"
    import dashboard_events
    dashboard_events.DASHBOARD_BASE = (self_base or settings.dashboard_base).rstrip("/")
    if plugin_base:
        import cad_mcp_server
        import spatial_cache
        cad_mcp_server.BASE = plugin_base.rstrip("/")
        spatial_cache.PLUGIN_BASE = plugin_base.rstrip("/")
    return Session(settings=settings, sequencer=sequencer, filler=filler, executor=ex,
                   model=backends.model_name(settings), tools_version=tools.version())
