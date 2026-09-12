"""The HTTP surface: one thin module per feature.

Routes contain NO logic — they validate input, call one subsystem, and
shape the response. If an if-statement in a route isn't about HTTP, it
belongs in the subsystem it's calling.
"""

# Each feature router, re-exported for app.py to include.
from seymour.routes.chat import router as chat_router            # noqa: F401
from seymour.routes.agent import router as agent_router          # noqa: F401
from seymour.routes.tasks import router as tasks_router          # noqa: F401
from seymour.routes.research import router as research_router    # noqa: F401
from seymour.routes.models import router as models_router        # noqa: F401
from seymour.routes.memory import router as memory_router        # noqa: F401
from seymour.routes.soul import router as soul_router            # noqa: F401
from seymour.routes.status import router as status_router        # noqa: F401
from seymour.routes.events import router as events_router        # noqa: F401
from seymour.routes.uploads import router as uploads_router      # noqa: F401
from seymour.routes.gallery import router as gallery_router      # noqa: F401
from seymour.routes.runs import router as runs_router            # noqa: F401
from seymour.routes.inference import router as inference_router  # noqa: F401
from seymour.routes.workspace import router as workspace_router  # noqa: F401
from seymour.routes.mcp import router as mcp_router              # noqa: F401
from seymour.routes.skills import router as skills_router        # noqa: F401
