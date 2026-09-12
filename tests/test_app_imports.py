"""Importing the app must succeed and every feature router must be
mounted (a missing import took the dev server down on 2026-09-03)."""


def test_app_module_imports_and_mounts_every_router():
    from seymour import app as app_module
    paths = set(app_module.app.openapi()["paths"])
    for prefix in ("/api/models", "/api/skills", "/api/mcp", "/api/inference", "/api/workspace/file"):
        assert any(p.startswith(prefix) for p in paths), prefix
