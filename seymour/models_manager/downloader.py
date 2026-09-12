"""Hugging Face integration: search, inspect, and download GGUF and MLX models.

Two shapes of model live on the hub: a GGUF repo holds several
single-file quants (pick ONE file), an MLX repo IS one quant (download
the whole repo — config.json, the safetensors shards, the tokenizer). The
search takes a backend and filters by the hub's own library tag; the
download takes a filename for GGUF and none for MLX.

Three lessons inherited from the reference implementation's battle scars
(see ACKNOWLEDGMENTS.md):

    1. Download into the standard HF hub cache layout (models--org--name/
       blobs + snapshots), NEVER a flat --local-dir: completed files are
       never fetched twice, a new revision re-fetches only changed blobs,
       and the author stays in the folder name. (Resume is per FILE:
       huggingface_hub 1.x discards a cancelled file's partial bytes, so
       a 5 GB shard restarts from zero — a multi-file repo still keeps
       every shard that finished.)
    2. Validate repo ids with an allowlist regex AND reject leading dashes
       (quoting does not stop a value being parsed as a CLI option).
    3. Progress parsing is one function, and completion is judged by the
       cache's shape (no *.incomplete blobs), not by trusting logs.

Downloads run in a worker thread (huggingface_hub is synchronous) and
report progress through the event bus, so the UI shows a live bar and a
closed tab costs nothing.
"""

import asyncio
import json
import logging
import re
import threading
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

from seymour.config import settings
from seymour.events import bus
from seymour.models_manager import hwfit

logger = logging.getLogger(__name__)

# Where downloads land: the hub cache INSIDE the Models directory, so all
# model storage lives in one visible place.
CACHE_DIR = settings.models_dir / "hub"

# repo ids look like "unsloth/Qwen3.6-35B-A3B-GGUF": one slash, safe chars,
# and no segment may start with '-' (option-injection defence).
_REPO_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*/[A-Za-z0-9][\w.\-]*$")

# One download at a time — these are tens of gigabytes.
_active: dict = {}          # {"repo": …, "file": …, "cancel": Event} or empty


def _validate_repo(repo_id: str) -> str:
    """Allowlist-validate a repo id or raise."""
    if not _REPO_RE.match(repo_id):
        raise ValueError(f"invalid repo id: {repo_id!r}")
    return repo_id


# The hub's library tags for the two formats (what `filter=` matches).
_BACKEND_FILTER = {"gguf": "gguf", "mlx": "mlx"}


def _is_drafter_repo(repo_id: str) -> bool:
    """MTP drafter repos are named `<model>-MTP-<quant>`; they are
    companions, not models to load on their own."""
    return "-mtp-" in repo_id.lower() or repo_id.lower().endswith("-mtp")


async def hf_search(query: str, backend: str = "gguf") -> list[dict]:
    """Search Hugging Face for repos in the chosen backend's format."""
    if backend not in _BACKEND_FILTER:
        raise ValueError(f"backend must be one of {sorted(_BACKEND_FILTER)}")
    api = HfApi()
    # The HF client is sync; run it off the event loop.
    models = await asyncio.to_thread(
        lambda: list(api.list_models(
            search=query, filter=_BACKEND_FILTER[backend], sort="downloads", limit=16,
        ))
    )
    return [
        {
            "repo": m.id,
            "downloads": m.downloads or 0,
            "likes": m.likes or 0,
            "backend": backend,
            # A drafter is listed so it can be fetched deliberately, but
            # the UI labels it — loading one alone does nothing.
            "drafter": backend == "mlx" and _is_drafter_repo(m.id),
        }
        for m in models
    ]


async def hf_drafter_for(repo_id: str) -> str | None:
    """The MTP drafter repo that matches an MLX repo, if the same author
    published one: `<name>-MTP-<quant>` next to `<name>-<quant>`."""
    repo_id = _validate_repo(repo_id)
    author, name = repo_id.split("/", 1)
    # "Qwen3.8-27B-4bit-DWQ": the quant is the bits; DWQ/AWQ are how the
    # bits were chosen, and the drafter is shared across those.
    stem = re.sub(r"-(dwq|awq)$", "", name, flags=re.I)
    match = re.match(r"^(.*?)-(\d+bit|bf16|fp16|mxfp4|mxfp8)$", stem, re.I)
    if not match:
        return None
    base, quant = match.group(1), match.group(2)
    api = HfApi()
    candidates = await asyncio.to_thread(
        lambda: list(api.list_models(author=author, search=f"{base}-MTP-{quant}", limit=5)))
    for candidate in candidates:
        if candidate.id.lower() == f"{author}/{base}-MTP-{quant}".lower():
            return candidate.id
    return None


async def hf_files(repo_id: str, backend: str = "gguf") -> list[dict]:
    """What can be downloaded from a repo, with sizes and fit verdicts.

    GGUF: one row per quant file (the quant picker). MLX: ONE row for
    the whole repo (an MLX repo is a single quant), sized as the sum of
    its safetensors shards, plus the matching drafter repo when one
    exists so the UI can offer to fetch both.
    """
    repo_id = _validate_repo(repo_id)
    api = HfApi()
    info = await asyncio.to_thread(
        lambda: api.model_info(repo_id, files_metadata=True)
    )
    if backend == "mlx":
        weights = sum((sib.size or 0) for sib in (info.siblings or [])
                      if sib.rfilename.endswith(".safetensors"))
        total = sum((sib.size or 0) for sib in (info.siblings or []))
        drafter = None if _is_drafter_repo(repo_id) else await hf_drafter_for(repo_id)
        # The fit deserves the model's real KV cost, and config.json is
        # 5 KB: fetch just that (pinned to this revision, into the very
        # snapshot the full download will fill) and read it like a local
        # checkpoint. A miss falls back to the params-B rule of thumb.
        kv_bytes = None
        try:
            from seymour.engine import mlxinfo
            config_path = await asyncio.to_thread(
                lambda: hf_hub_download(repo_id, "config.json", cache_dir=CACHE_DIR,
                                        revision=info.sha))
            facts = mlxinfo.parse_config(json.loads(Path(config_path).read_text()))
            kv_bytes = facts["kv_bytes_per_token"] or None
        except Exception as error:                     # noqa: BLE001 — a fit hint, never fatal
            logger.info("config.json prefetch for %s failed: %s", repo_id, error)
        fit = None
        if weights:
            from seymour.models_manager.registry import engine_settings
            chosen = engine_settings()
            fit = hwfit.fit_for(weights, repo_id.split("/")[-1], ctx=chosen["ctx_size"],
                                kv_bytes_per_token=kv_bytes,
                                sequences=int(chosen["mlx_decode_concurrency"]),
                                extra_gb=float(chosen["mlx_prompt_cache_gb"]))
        return [{
            "file": "",                       # "" = the whole repo
            "size_bytes": total,
            "size": f"{total / 1e9:.1f} GB" if total else "?",
            "fit": fit,
            "drafter_repo": drafter,
            "is_drafter": _is_drafter_repo(repo_id),
            "revision": info.sha,
        }]
    files = []
    for sibling in info.siblings or []:
        name = sibling.rfilename
        if not name.lower().endswith(".gguf"):
            continue
        # Hide non-first shards; the first shard is what gets downloaded
        # by name and llama-server finds the rest… except we download each
        # file individually, so list ONLY first shards and mmproj files.
        if "-of-" in name and "-00001-of-" not in name:
            continue
        size = sibling.size or 0
        files.append({
            "file": name,
            "size_bytes": size,
            "size": f"{size / 1e9:.1f} GB" if size else "?",
            # Fit BEFORE downloading: the size is in the HF listing, so
            # the verdict costs nothing and saves a 30 GB mistake.
            "fit": hwfit.fit_for(size, name) if size else None,
        })
    return files


def start_download(repo_id: str, filename: str = "") -> dict:
    """Begin a download in a worker thread. Progress → event bus.

    `filename` names ONE .gguf; empty means the WHOLE repo (an MLX
    checkpoint), fetched file by file into the same resumable hub cache
    layout — a cancelled or crashed transfer resumes per file, and the
    registry only lists the snapshot once config.json and every shard
    named in the safetensors index are present.
    """
    repo_id = _validate_repo(repo_id)
    # Filenames come from hf_files; still, keep the same paranoia.
    if filename and (filename.startswith("-") or "/" in filename or ".." in filename):
        raise ValueError(f"invalid filename: {filename!r}")
    if _active:
        raise RuntimeError("another download is already running")

    cancel = threading.Event()
    _active.update({"repo": repo_id, "file": filename or "(whole repo)", "cancel": cancel})
    # Grab the running loop NOW (we're on it) so the worker thread can
    # schedule bus events back onto it safely.
    loop = asyncio.get_running_loop()

    def _publish(type_: str, **data) -> None:
        """Thread-safe bridge: worker thread → event loop → bus."""
        loop.call_soon_threadsafe(
            bus.publish, "download", type_,
            repo=repo_id, file=filename, **data,
        )

    # Whole-repo downloads add up across files: these two numbers are
    # the repo's total and what landed so far (the bar shows the sum).
    repo_total = {"bytes": 0, "done": 0}

    def _worker() -> None:
        """The blocking download, in its own thread."""
        try:
            # A tqdm stand-in: huggingface_hub calls this exactly like tqdm,
            # and we forward the byte counts as progress events (throttled
            # to whole-percent changes so the bus isn't flooded).
            class ProgressBar:
                def __init__(self, *args, **kwargs):
                    self.total = kwargs.get("total") or 0
                    self.n = 0
                    self._last_pct = -1

                def update(self, n=1):
                    self.n += n
                    if cancel.is_set():
                        # Raising here aborts the transfer. This FILE's
                        # partial bytes are discarded (huggingface_hub 1.x);
                        # every file that already finished stays.
                        raise RuntimeError("download cancelled")
                    # Report against the WHOLE job: one file, or the repo.
                    total = repo_total["bytes"] or self.total
                    done = repo_total["done"] + self.n
                    pct = int(done * 100 / total) if total else 0
                    if pct != self._last_pct:
                        self._last_pct = pct
                        _publish("progress", percent=pct,
                                 done_bytes=done, total_bytes=total)

                # The rest of the tqdm surface, as no-ops.
                def close(self): pass
                def __enter__(self): return self
                def __exit__(self, *exc): return False
                def set_description(self, *a, **k): pass
                def set_postfix(self, *a, **k): pass
                def refresh(self): pass
                @property
                def disable(self): return False
                @disable.setter
                def disable(self, value): pass

            if filename:
                path = hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    cache_dir=CACHE_DIR,        # the resume-friendly hub layout
                    tqdm_class=ProgressBar,     # our progress bridge
                )
            else:
                # The whole repo, one file at a time, in the same layout.
                info = HfApi().model_info(repo_id, files_metadata=True)
                revision = info.sha                 # PINNED: a commit landing
                wanted = [sib for sib in (info.siblings or [])   # mid-download must not
                          if not sib.rfilename.startswith(".git")]   # split the shards
                repo_total["bytes"] = sum((sib.size or 0) for sib in wanted)
                path = None
                # Small files first so config/tokenizer land before the
                # shards — an interrupted repo is then recognisably partial
                # (the registry checks the index against the shards).
                for sib in sorted(wanted, key=lambda x: (x.size or 0)):
                    if cancel.is_set():
                        raise RuntimeError("download cancelled")
                    got = hf_hub_download(repo_id=repo_id, filename=sib.rfilename,
                                          cache_dir=CACHE_DIR, tqdm_class=ProgressBar,
                                          revision=revision)
                    repo_total["done"] += sib.size or 0
                    path = Path(got).parent
                _publish("progress", percent=100, done_bytes=repo_total["done"],
                         total_bytes=repo_total["bytes"])
            _publish("done", path=str(path))
            logger.info("downloaded %s/%s → %s", repo_id, filename or "(whole repo)", path)
        except Exception as error:
            kind = "cancelled" if cancel.is_set() else "failed"
            _publish(kind, error=str(error))
            logger.info("download %s: %s", kind, error)
        finally:
            _active.clear()

    threading.Thread(target=_worker, name="hf-download", daemon=True).start()
    return {"repo": repo_id, "file": filename, "started": True}


def cancel_download() -> bool:
    """Ask the active download to stop (its partial blob remains, resumable)."""
    if not _active:
        return False
    _active["cancel"].set()
    return True


def download_status() -> dict:
    """Is anything downloading right now? (UI poll on page load.)"""
    return {"active": bool(_active),
            "repo": _active.get("repo"), "file": _active.get("file")}
