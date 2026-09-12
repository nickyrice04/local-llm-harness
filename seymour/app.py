"""The application shell. Wiring only — no logic lives here.

Startup order is the dependency order, and it is the one startup step the
reference implementation never needed: launch and HANDSHAKE the inference
engine before anything that might want to use it.

    db → engine → handshake → scheduler → embedder → agent → routes → UI

One deliberate exception: if no model file exists yet (a fresh install),
Seymour boots anyway in "setup mode" — the Models tab must work so the
user can download their first model; chat simply answers 503 until then.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from seymour import live_runs, runtime
from seymour.agent.discrete import discrete
from seymour.agent.manager import AgentManager
from seymour.config import settings
from seymour.db import get_state, init_db
from seymour.engine.llamacpp import LlamaCppEngine
from seymour.memory import embedder as emb
from seymour.memory.embedder import LlamaEmbedder
from seymour.models_manager.registry import active_model_path
from seymour.routes import (
    agent_router,
    chat_router,
    events_router,
    gallery_router,
    memory_router,
    models_router,
    research_router,
    runs_router,
    soul_router,
    status_router,
    tasks_router,
    uploads_router,
    inference_router,
    workspace_router,
    mcp_router,
    skills_router,
)
from seymour.scheduler.core import Scheduler

# One logging setup for the whole backend: level, and a format that names
# the module — so a line can be traced to its subsystem at a glance.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("seymour")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup above the yield, shutdown below — visible symmetry:
    anything started above is stopped below."""
    logger.info("Seymour starting")
    # 1. The database (idempotent create of missing tables).
    init_db()

    # 2. The engine + handshake + scheduler — IF a model exists yet, and
    #    the user didn't deliberately Unload last session (that choice
    #    must survive a restart, or Unload would be a lie).
    model = active_model_path()
    if get_state("engine_autoload", "yes") == "no":
        logger.info("engine autoload is off (model was unloaded) — "
                    "load one from the Models tab")
    elif model.exists():
        # The weights' KIND picks the engine: a checkpoint folder is MLX,
        # a .gguf is llama.cpp. Same start/handshake/scheduler after that.
        if model.is_dir():
            from seymour.engine.mlx import MlxEngine
            engine = MlxEngine(model_path=model)
        else:
            engine = LlamaCppEngine(model_path=model)
        await engine.start()              # blocks until weights are resident
        caps = await engine.capabilities()  # the four probes, run once
        runtime.engine = engine
        runtime.caps = caps
        runtime.scheduler = Scheduler(engine, caps)
    else:
        # Setup mode: no model yet. The UI's Models tab handles it from here.
        logger.warning("no model at %s — booting in setup mode", model)

    # 3. The memory embedder: the real one when its model exists, the
    #    dependency-free fallback otherwise (memory must never block boot).
    llama_embedder = None
    if settings.embed_model_path.exists():
        try:
            llama_embedder = LlamaEmbedder()
            await llama_embedder.start()
            emb.use(llama_embedder)
        except Exception:
            logger.exception("embedding server failed; using hashing embedder")
            llama_embedder = None

    # 4. The primary agent — resumes whatever the last session left off.
    runtime.agent = AgentManager()
    await runtime.agent.startup()

    # 5. The discrete task pool (chat-started Tier 2 jobs): anything left
    #    'running' by the last session re-pauses honestly.
    await discrete.startup()
    # MCP servers the person configured: connect, register their tools.
    from seymour import mcp
    await mcp.manager.startup()

    logger.info("ready at http://%s:%s", settings.host, settings.port)
    yield                                 # ← the application runs here
    logger.info("Seymour shutting down")

    # Shutdown mirrors startup, in reverse.
    from seymour import mcp
    await mcp.manager.shutdown()          # child servers stop with us
    await live_runs.shutdown()            # detached chat runs stop; partial replies persist
    from seymour.tools import jobs, shell as shell_tools
    await jobs.kill_all()                 # background jobs (servers, builds) die with the app
    await shell_tools.close_all_shells()  # persistent shell sessions too
    await discrete.shutdown()             # discrete jobs re-pause next boot
    await runtime.agent.shutdown()        # checkpoint stands; task resumes next boot
    if llama_embedder is not None:
        await llama_embedder.stop()
    if runtime.engine is not None:
        await runtime.engine.stop()       # reclaim the 35 GB — never orphan it


# The FastAPI app itself: title, lifecycle, routes.
app = FastAPI(title="Seymour", lifespan=lifespan)
# Every feature router (thin; the subsystems do the work).
app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(tasks_router)
app.include_router(research_router)
app.include_router(runs_router)
app.include_router(models_router)
app.include_router(inference_router)
app.include_router(workspace_router)
app.include_router(mcp_router)
app.include_router(skills_router)
app.include_router(memory_router)
app.include_router(soul_router)
app.include_router(status_router)
app.include_router(events_router)
app.include_router(uploads_router)
app.include_router(gallery_router)

# Static files mounted LAST: html=True serves index.html at "/", and a
# catch-all mounted earlier would shadow every /api route above.
app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True),
          name="static")
