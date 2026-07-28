"""VRAM Director: single OpenAI-compatible endpoint for the NUC appliance.

Discovers models from per-folder config.toml files, launches the matching
inference engine container through the host Podman socket, and enforces
strict VRAM isolation (only one engine runs at a time).
"""

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-wrapper")

PODMAN_URL = os.getenv("PODMAN_URL", "unix:///run/podman/podman.sock")
MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
MODELS_HOST_DIR = os.getenv("MODELS_HOST_DIR", "")
ENGINE_NETWORK = os.getenv("ENGINE_NETWORK", "nuc-infra_ai-isolated-net")
GH_USER = os.getenv("GH_USER", "")
API_KEY = os.getenv("WRAPPER_API_KEY", "")
ALLOW_ANONYMOUS = os.getenv("ALLOW_ANONYMOUS", "").lower() == "true"
# Multi-key store: the env API_KEY (bootstrap, not revocable via API) plus any
# persisted keys in KEYS_FILE (minted via POST /admin/keys, individually revocable).
KEYS_FILE = Path(os.getenv("KEYS_FILE", "/app/api_keys.txt"))


def _load_keys() -> set:
    """Valid bearer keys = the env API_KEY + any persisted in KEYS_FILE."""
    keys = set()
    if API_KEY:
        keys.add(API_KEY)
    try:
        if KEYS_FILE.exists():
            for line in KEYS_FILE.read_text().splitlines():
                k = line.strip()
                if k and not k.startswith("#"):
                    keys.add(k)
    except OSError as exc:
        log.error("Could not read KEYS_FILE %s: %s", KEYS_FILE, exc)
    return keys
ENGINE_CONTAINER = "llama-engine"
ENGINE_PORT = 8080

def _parse_engine_ports(env: str) -> dict:
    # Per-engine published host port. Engines are mutually exclusive on the GPU,
    # so only the active engine's port is live at a time. Override via ENGINE_PORTS.
    out = {"llama-cuda": 5121, "llama-atomic": 5122, "llama-tom": 5123}
    for item in (env or "").split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            out[k.strip()] = int(v.strip())
    return out
ENGINE_PORTS = _parse_engine_ports(os.getenv("ENGINE_PORTS", ""))
VRAM_COOLDOWN_S = 1.5
DEFAULT_LOAD_TIMEOUT_S = 30

WRAPPER_MODE = os.getenv("WRAPPER_MODE", "dynamic").lower()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "")

# Fix 2: allowlist for [args] keys
_ARGS_ALLOWLIST = {"ctx_size", "n_gpu_layers", "flash_attn", "draft_model",
                   "draft_max", "draft_min", "load_timeout", "mmproj", "extra"}
# Regex for extra items: --flag[=value] or bare value
_EXTRA_FLAG_RE = re.compile(r'^--[A-Za-z0-9][A-Za-z0-9_.:=-]*$')
_EXTRA_BARE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:=-]*$')
# Regex for valid alias/folder names
_ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

active_alias: str | None = None

# Engine guard: one Condition protects active_alias transitions, the in-flight
# request count and the single-swapper flag. Swap I/O runs OUTSIDE the guard
# so in-flight requests can drain while the swapper waits.
_guard = asyncio.Condition()
_inflight: int = 0
_swapping: bool = False

_registry: dict[str, dict] = {}
# Fix 3: per-file mtime signature instead of directory mtime
_registry_sig: dict[str, float] = {}
_schema_cache: dict[str, set[str]] = {}


def _validate_args(args: dict, cfg_path: Path | str, schema_flags: set[str] | None = None) -> bool:
    """Return True if [args] is valid; log and return False otherwise."""
    unknown = set(args) - _ARGS_ALLOWLIST
    if unknown:
        log.error("Config %s: unknown [args] keys %s — excluded from registry", cfg_path, unknown)
        return False
    extra = args.get("extra")
    if extra is not None:
        if not isinstance(extra, list):
            log.error("Config %s: [args].extra must be a list of strings — excluded", cfg_path)
            return False
        for item in extra:
            if not isinstance(item, str):
                log.error("Config %s: [args].extra items must be strings — excluded", cfg_path)
                return False
            if not (_EXTRA_FLAG_RE.match(item) or _EXTRA_BARE_RE.match(item)):
                log.error("Config %s: [args].extra item %r is not a safe flag/value — excluded",
                          cfg_path, item)
                return False
            if schema_flags is not None and item.startswith("-"):
                flag_name = item.split("=", 1)[0]
                if flag_name not in schema_flags:
                    log.error("Config %s: flag %r is not supported by engine schema", cfg_path, flag_name)
                    return False
    draft = args.get("draft_model")
    if draft is not None and ("/" in draft or ".." in draft):
        log.error("Config %s: [args].draft_model %r must be a filename, not a path — excluded",
                  cfg_path, draft)
        return False
    return True


async def get_engine_schema(engine: str) -> set[str] | None:
    """Lazily load and digest-cache the engine's flags.toml schema using CPU-only podman inspection."""
    image = f"ghcr.io/{GH_USER}/{engine}:latest"
    try:
        inspect_out = await podman("image", "inspect", "--format", "{{.Digest}}", image, check=False)
        digest = inspect_out.strip()
        if not digest or "sha256:" not in digest:
            digest = image
    except Exception as exc:
        log.warning("Could not inspect image digest for %s: %s", engine, exc)
        digest = image

    if digest in _schema_cache:
        return _schema_cache[digest]

    try:
        out = await podman(
            "run", "--rm", "--entrypoint", "/bin/cat", image, "/etc/llama-engine/flags.toml",
            check=False
        )
        if not out.strip() or "flags" not in out:
            log.warning("No valid flags.toml found for engine %s (digest %s); falling back to legacy allowlist", engine, digest)
            return None
        cfg = tomllib.loads(out)
        flags_list = cfg.get("flags", {}).get("flags", [])
        if isinstance(flags_list, list):
            flag_set = set(flags_list)
            _schema_cache[digest] = flag_set
            return flag_set
    except Exception as exc:
        log.warning("Failed to load schema for engine %s (digest %s): %s; falling back to legacy allowlist", engine, digest, exc)

    return None


def scan_registry() -> dict[str, dict]:
    """Scan MODELS_DIR for <folder>/config.toml files and build the registry."""
    global _registry, _registry_sig

    # Fix 3: build a per-file signature and compare
    cfg_paths = sorted(MODELS_DIR.glob("*/config.toml"))
    try:
        sig = {str(p): p.stat().st_mtime for p in cfg_paths}
    except FileNotFoundError:
        return {}

    if sig == _registry_sig:
        return _registry

    counts: dict[str, int] = {}
    candidates: list[tuple[str, dict]] = []

    for cfg_path in cfg_paths:
        try:
            with cfg_path.open("rb") as f:
                cfg = tomllib.load(f)
            model = cfg["model"]
            alias = model["alias"]
            folder = cfg_path.parent.name

            # Fix 2: validate alias format
            if not _ALIAS_RE.match(alias):
                log.error("Config %s: alias %r is not a safe identifier — excluded", cfg_path, alias)
                continue

            # Fix 2: validate folder name (no path separators)
            if "/" in folder or "\\" in folder or ".." in folder:
                log.error("Config %s: folder name %r contains path separators — excluded",
                          cfg_path, folder)
                continue

            # Fix 2: validate file field
            file_val = model.get("file", "model.gguf")
            if "/" in file_val or ".." in file_val:
                log.error("Config %s: model.file %r must be a filename, not a path — excluded",
                          cfg_path, file_val)
                continue

            args = cfg.get("args", {})
            if not _validate_args(args, cfg_path):
                continue

            counts[alias] = counts.get(alias, 0) + 1
            candidates.append((alias, {
                "alias": alias,
                "engine": model["engine"],
                "folder": folder,
                "file": file_val,
                "args": args,
            }))
        except Exception as exc:
            log.warning("Invalid config %s: %s", cfg_path, exc)

    # Fix 4: exclude ALL entries with a duplicate alias
    registry: dict[str, dict] = {}
    for alias, entry in candidates:
        if counts[alias] > 1:
            log.error("Duplicate alias %r found in multiple configs — ALL excluded", alias)
            continue
        registry[alias] = entry

    _registry, _registry_sig = registry, sig
    log.info("Model registry: %s", list(registry))
    return registry


def build_engine_command(entry: dict) -> list[str]:
    """Translate a config.toml [args] table into llama-server flags."""
    folder = f"/models/{entry['folder']}"
    args = entry["args"]
    cmd = ["llama-server", "-m", f"{folder}/{entry['file']}",
           "--host", "0.0.0.0", "--port", str(ENGINE_PORT)]
    if "ctx_size" in args:
        cmd += ["-c", str(args["ctx_size"])]
    if "n_gpu_layers" in args:
        cmd += ["-ngl", str(args["n_gpu_layers"])]
    if args.get("flash_attn"):
        # These llama.cpp forks take --flash-attn [on|off|auto], not a bare flag.
        cmd += ["--flash-attn", "on"]
    if "draft_model" in args:
        cmd += ["--model-draft", f"{folder}/{args['draft_model']}"]
    if "draft_max" in args:
        cmd += ["--draft-max", str(args["draft_max"])]
    if "draft_min" in args:
        cmd += ["--draft-min", str(args["draft_min"])]
    if "mmproj" in args:
        mm_val = str(args["mmproj"])
        mm_path = mm_val if mm_val.startswith("/") else f"{folder}/{mm_val}"
        cmd += ["--mmproj", mm_path]
    cmd += [str(x) for x in args.get("extra", [])]
    return cmd


async def podman(*args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "podman", "--url", PODMAN_URL, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    # Fix 6: include both stdout and stderr in the error message
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"podman {' '.join(args[:2])} failed "
            f"(rc={proc.returncode}): {err.decode().strip()} | stdout: {out.decode().strip()}"
        )
    return out.decode()


async def stop_engine() -> None:
    global active_alias
    # Fix 5: capture output, verify container is gone, set active_alias only after confirmed
    await podman("stop", "--ignore", "-t", "10", ENGINE_CONTAINER, check=False)
    await podman("rm", "--ignore", "-f", ENGINE_CONTAINER, check=False)
    # Verify the container is actually gone
    proc = await asyncio.create_subprocess_exec(
        "podman", "--url", PODMAN_URL, "container", "exists", ENGINE_CONTAINER,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    if proc.returncode == 0:
        # Container still exists
        raise RuntimeError(f"Container {ENGINE_CONTAINER!r} still exists after stop+rm")
    active_alias = None


async def start_engine(entry: dict) -> None:
    """Stop the active engine, wait out the VRAM cooldown, start the target."""
    global active_alias
    await stop_engine()
    # NVIDIA driver needs time to release the eGPU memory map
    await asyncio.sleep(VRAM_COOLDOWN_S)

    models_host = MODELS_HOST_DIR or "/models"
    image = f"ghcr.io/{GH_USER}/{entry['engine']}:latest"
    port = ENGINE_PORTS.get(entry["engine"])
    run_args = [
        "run", "--rm", "-d",
        "--name", ENGINE_CONTAINER,
        "--network", ENGINE_NETWORK,
        "--device", "nvidia.com/gpu=all",
        # SELinux (enforcing on uCore): container_t is denied the nvidia device
        # nodes (xserver_misc_device_t). label=disable lets the engine reach the GPU.
        "--security-opt", "label=disable",
    ]
    if port:  # publish the engine on its dedicated host port (5121-5123)
        run_args += ["-p", f"{port}:{ENGINE_PORT}"]
    run_args += ["-v", f"{models_host}:/models:ro,z", image, *build_engine_command(entry)]
    await podman(*run_args)

    timeout = float(entry["args"].get("load_timeout", DEFAULT_LOAD_TIMEOUT_S))
    deadline = asyncio.get_running_loop().time() + timeout  # Fix 8: get_running_loop
    async with httpx.AsyncClient() as client:
        try:
            while True:
                try:
                    r = await client.get(
                        f"http://{ENGINE_CONTAINER}:{ENGINE_PORT}/health", timeout=2.0)
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() > deadline:  # Fix 8
                    await stop_engine()
                    raise HTTPException(503, f"Engine for {entry['alias']!r} failed health check")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # Shield the cleanup so a second cancellation cannot interrupt it,
            # and never let a cleanup failure replace the cancellation signal.
            try:
                await asyncio.shield(stop_engine())
            except Exception as exc:
                log.error("Cleanup after cancelled engine start failed: %s", exc)
            raise

    # active_alias is written by acquire_engine under _guard
    log.info("Engine ready: %s (%s)", entry["alias"], entry["engine"])


async def acquire_engine(alias: str, entry: dict) -> None:
    """Ensure `alias` is the active engine and register this request as in-flight.

    Same-alias requests only bump the counter. A swap drains in-flight
    requests first, runs with the guard released, and admits no new
    requests until it finishes (single-swapper flag).
    """
    schema = await get_engine_schema(entry["engine"])
    if not _validate_args(entry["args"], f"model:{alias}", schema_flags=schema):
        unsupported = []
        if schema is not None and "extra" in entry["args"]:
            for item in entry["args"].get("extra", []):
                if isinstance(item, str) and item.startswith("-"):
                    flag_name = item.split("=", 1)[0]
                    if flag_name not in schema:
                        unsupported.append(flag_name)
        err_msg = f"Invalid arguments for engine {entry['engine']!r}"
        if unsupported:
            err_msg += f": unsupported flag {unsupported[0]!r}"
    if WRAPPER_MODE == "persistent" and DEFAULT_MODEL and alias != DEFAULT_MODEL:
        raise HTTPException(
            400,
            f"Wrapper running in persistent mode locked to model {DEFAULT_MODEL!r}. "
            f"Requested model {alias!r} requires setting WRAPPER_MODE=dynamic."
        )

    global _swapping, _inflight, active_alias
    while True:
        async with _guard:
            if _swapping:
                await _guard.wait_for(lambda: not _swapping)
                continue
            if active_alias == alias:
                _inflight += 1
                return
            _swapping = True
            try:
                while _inflight > 0:
                    await _guard.wait()
            except BaseException:
                # Cancellation while draining: reset synchronously — the
                # guard is still held here, so this cannot be interrupted.
                _swapping = False
                _guard.notify_all()
                raise
        try:
            log.info("Swapping engine: %s -> %s", active_alias, alias)
            await start_engine(entry)
        except BaseException:
            await _clear_swapping()
            raise
        async with _guard:
            _swapping = False
            _inflight += 1
            active_alias = alias
            _guard.notify_all()
        return


async def _clear_swapping() -> None:
    """Reset the swap flag; shielded so a second cancellation cannot leave it stuck."""
    async def _do():
        global _swapping
        async with _guard:
            _swapping = False
            _guard.notify_all()
    await asyncio.shield(_do())


async def release_engine() -> None:
    global _inflight
    async with _guard:
        _inflight -= 1
        _guard.notify_all()


def check_auth(request: Request) -> None:
    valid = _load_keys()
    if not valid:  # no keys configured at all → open mode
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if token in valid:
        return
    raise HTTPException(401, "Invalid or missing API key")


# Fix 10: lifespan handler replacing deprecated @app.on_event("startup")
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fix 7: startup validation
    if not MODELS_HOST_DIR:
        log.warning(
            "WARNING: MODELS_HOST_DIR is unset or empty — engine containers will mount /models "
            "which may not exist on the host. Set MODELS_HOST_DIR to the absolute host path."
        )
    # Verify the engine network exists
    proc = await asyncio.create_subprocess_exec(
        "podman", "--url", PODMAN_URL, "network", "exists", ENGINE_NETWORK,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"Podman network {ENGINE_NETWORK!r} does not exist - continuing anyway")

    # Refuse to start if API key is empty and anonymous is not explicitly allowed
    if not API_KEY and not ALLOW_ANONYMOUS:
        raise RuntimeError(
            "WRAPPER_API_KEY is empty and ALLOW_ANONYMOUS is not 'true'. "
            "Refusing to run open. Set WRAPPER_API_KEY or set ALLOW_ANONYMOUS=true to override.")

    scan_registry()
    # Clean up any engine left over from a previous wrapper run
    try:
        await stop_engine()
    except RuntimeError as exc:
        log.warning("Startup cleanup: %s", exc)

    if WRAPPER_MODE == "persistent" and DEFAULT_MODEL:
        registry = scan_registry()
        if DEFAULT_MODEL in registry:
            log.info("WRAPPER_MODE='persistent': Pre-loading default model %r...", DEFAULT_MODEL)
            try:
                await start_engine(registry[DEFAULT_MODEL])
                global active_alias
                async with _guard:
                    active_alias = DEFAULT_MODEL
                log.info("WRAPPER_MODE='persistent': Default model %r is ready", DEFAULT_MODEL)
            except Exception as exc:
                log.error("Failed to pre-load persistent model %r: %s", DEFAULT_MODEL, exc)
        else:
            log.warning("DEFAULT_MODEL %r not in registry: %s", DEFAULT_MODEL, list(registry))

    yield

    # Deterministic shutdown cleanup, independent of task-cancellation races
    try:
        await stop_engine()
    except RuntimeError as exc:
        log.warning("Shutdown cleanup: %s", exc)


app = FastAPI(title="ai-wrapper", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Fix 11: /health does NOT expose active_model (unauthenticated endpoint)
@app.get("/health")
async def health():
    return {"status": "ok"}


# Fix 11: authenticated /v1/status exposes active_model
@app.get("/v1/status")
async def status(request: Request):
    check_auth(request)
    return {"status": "ok", "active_model": active_alias}


@app.get("/v1/models")
@app.get("/models")
@app.get("/api/models")
async def list_models(request: Request):
    check_auth(request)
    registry = scan_registry()
    items = []
    for alias, entry in registry.items():
        has_vision = "mmproj" in entry.get("args", {})
        m_item = {
            "id": alias,
            "name": alias,
            "object": "model",
            "owned_by": entry.get("engine", "nuc"),
            "supports_vision": has_vision,
            "capabilities": {"vision": has_vision},
            "modalities": ["text", "image"] if has_vision else ["text"],
        }
        items.append(m_item)
    return {
        "object": "list",
        "data": items,
        "models": items,
    }


@app.get("/api/tags")
async def ollama_tags(request: Request):
    check_auth(request)
    registry = scan_registry()
    return {
        "models": [
            {
                "name": alias,
                "model": alias,
                "modified_at": "2026-07-28T00:00:00Z",
                "size": 7500000000,
                "digest": "sha256:0000",
                "details": {
                    "format": "gguf",
                    "family": "llama",
                    "parameter_size": "12B",
                    "quantization_level": "Q4_K_M"
                }
            }
            for alias, entry in registry.items()
        ]
    }


# --- API key management (admin) ---
# W1 fix: /admin/keys* require the bootstrap env key (admin), not any valid key.
# W3 fix: serialize KEYS_FILE read-modify-write with a lock (no TOCTOU race).
_keys_lock = asyncio.Lock()


def require_admin(request: Request) -> None:
    """Admin actions (mint/list/revoke keys) require the bootstrap env key."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not API_KEY or token != API_KEY:
        raise HTTPException(403, "Admin privileges required (bootstrap key)")


@app.post("/admin/keys")
async def mint_key(request: Request):
    """Mint a new sk-llama-<random> API key. Admin-only (bootstrap key)."""
    require_admin(request)
    new_key = "sk-llama-" + secrets.token_hex(24)
    async with _keys_lock:
        try:
            KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with KEYS_FILE.open("a") as f:
                f.write(new_key + "\n")
        except OSError as exc:
            raise HTTPException(500, f"Could not persist key: {exc}")
    log.info("Issued new API key via /admin/keys")
    return {"key": new_key}


@app.get("/admin/keys")
async def list_keys(request: Request):
    require_admin(request)
    keys = sorted(_load_keys())
    return {"count": len(keys), "keys": [k[:24] + "…" for k in keys]}


@app.delete("/admin/keys/{key}")
async def revoke_key(key: str, request: Request):
    # W2 fix: revoke by EXACT key match (no prefix over-revoke / DoS).
    require_admin(request)
    if len(key) < 32:
        raise HTTPException(400, "Provide the full key to revoke (exact match)")
    async with _keys_lock:
        file_keys = [k for k in _load_keys() if k != API_KEY]
        remaining = [k for k in file_keys if k != key]
        removed = len(file_keys) - len(remaining)
        try:
            KEYS_FILE.write_text("\n".join(remaining) + ("\n" if remaining else ""))
        except OSError as exc:
            raise HTTPException(500, f"Could not persist: {exc}")
    return {"revoked": removed, "remaining_file_keys": len(remaining)}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    check_auth(request)
    body = await request.json()
    alias = body.get("model")
    registry = scan_registry()
    if alias not in registry:
        raise HTTPException(404, f"Unknown model {alias!r}. Available: {list(registry)}")

    # Swap (if needed) and register this request as in-flight, atomically
    await acquire_engine(alias, registry[alias])

    upstream = f"http://{ENGINE_CONTAINER}:{ENGINE_PORT}/v1/chat/completions"
    if body.get("stream"):
        # The generator owns the in-flight slot: it releases it when the
        # stream ends, errors, or the client disconnects.
        return StreamingResponse(
            stream_upstream(upstream, body, request),
            media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            r = await client.post(upstream, json=body)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type="application/json")
    finally:
        await release_engine()


async def stream_upstream(url: str, body: dict, request: Request):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            async with client.stream("POST", url, json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread())[:4096].decode(errors="replace")
                    yield f"data: {json.dumps({'error': detail})}\n\n".encode()
                    return
                async for chunk in r.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
    finally:
        await release_engine()
