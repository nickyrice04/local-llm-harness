"""Model management: what's on disk, what's running, what's downloadable.

    registry.py   — find local .gguf files, track the active one, and swap
                    models (stop engine → start with new weights → re-run
                    the handshake → rebuild the scheduler)
    downloader.py — search Hugging Face and download GGUF files, with live
                    progress on the event bus and resume-friendly storage
"""

# Public surface.
from seymour.models_manager.registry import activate_model, list_local_models  # noqa: F401
from seymour.models_manager.downloader import hf_search, hf_files, start_download  # noqa: F401
