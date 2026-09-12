"""Importing the app must succeed and every feature router must be
mounted (a missing import took the dev server down on 2026-09-03)."""


def test_app_module_imports_and_mounts_every_router():
    from seymour import app as app_module
    paths = set(app_module.app.openapi()["paths"])
    for prefix in ("/api/models", "/api/skills", "/api/mcp", "/api/inference", "/api/workspace/file"):
        assert any(p.startswith(prefix) for p in paths), prefix


def test_the_lifespan_decorator_sits_on_lifespan_and_the_profile_helper_is_awaitable():
    """Measured 2026-09-11: a helper inserted between @asynccontextmanager
    and lifespan took the decorator, and the app crashed at boot with a
    model loaded (no test exercises that path — this one guards the shape)."""
    import inspect
    from seymour import app
    assert inspect.iscoroutinefunction(app._measure_profile)
    assert not inspect.isasyncgenfunction(app.lifespan)          # decorated: a context-manager factory
    assert hasattr(app.lifespan(app.app), "__aenter__")
