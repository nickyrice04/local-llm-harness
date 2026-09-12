"""Compatibility shim: the tool registry moved to `seymour.tools`.

Every capability now lives in the `seymour/tools/` package (one module
per family) so the chat executor, the primary agent and the discrete
task pool share ONE catalog. This module keeps the old import path
alive for callers and tests that grew up with `seymour.agent.tools`;
new code should import `seymour.tools` directly.
"""

from seymour.tools import (            # noqa: F401  (re-exports)
    MAX_RESULT_CHARS,
    TOOLS,
    Tool,
    bounded,
    catalog,
    describe_call,
    execute,
    is_error,
    looks_like_call,
    parse_call,
    render_catalog,
)
from seymour.tools.web import (        # noqa: F401  (research's entry points)
    fetch_document,
    fetch_page,
    fetch_page_with_meta,
    public_ips,
    search_results,
    web_search,
)
