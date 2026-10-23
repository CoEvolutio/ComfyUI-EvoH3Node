"""EvoH3 MiniMax H3 video generation nodes for ComfyUI.

Why this pack exists instead of reusing an existing MiniMax H3 node pack:
EvoH3 does not implement ``/v1/files/upload`` (its docs list ``/v1/files``
as "501 Not Implemented", and the path returns 404). Every node pack that
uploads local media therefore fails against this relay. EvoH3's
OpenAI-compatible flat entry ``POST /v1/videos`` *does* accept a local file as
multipart ``input_reference``, so this pack talks to that entry for local
media and to the JSON form of the same entry for everything else.

All requests are made from the ComfyUI host process. The API key is only ever
put in an ``Authorization`` header; it is never logged.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import wave
from io import BytesIO

import aiohttp
from typing_extensions import override

from comfy.utils import ProgressBar
from comfy_api.latest import IO, ComfyExtension
from comfy_api.latest._input_impl import VideoFromFile
from comfy_api_nodes.util._helpers import is_processing_interrupted
from comfy_api_nodes.util.common_exceptions import ProcessingInterrupted
from comfy_api_nodes.util.conversions import tensor_to_bytesio

# Private helper that renders the status line under a running node. Guarded so a
# future ComfyUI that moves it degrades to "no status text" instead of breaking
# the whole pack.
try:
    from comfy_api_nodes.util.client import _display_text
except Exception:  # pragma: no cover - depends on the ComfyUI build
    _display_text = None

log = logging.getLogger(__name__)

CATEGORY = "EvoH3"

# Placeholder on purpose: every deployment is issued its own endpoint, so shipping a
# real default would look like it works out of the box and then fail with a DNS or 401
# error. The check in `_resolve_base_url` turns that into an actionable message.
DEFAULT_BASE_URL = "https://your-gateway.example.com"
PLACEHOLDER_HOSTS = ("your-gateway.example.com", "example.com")

KEY_ENV = "EVOH3_API_KEY"
# Pre-rename variable kept working so existing setups do not break silently.
LEGACY_KEY_ENV = "CONFLUIA_API_KEY"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 1800.0  # 30 min hard cap

CATALOG_TTL = 300.0  # seconds to reuse a model catalogue

# ---------------------------------------------------------------------------
# Model selection
#
# Resolution is a property of the *model id* on this relay — it registers
# `minimax-h3-768p` and `minimax-h3-2k`, and rejects the `MiniMax-H3` name that
# MiniMax's own docs use. So the 2K/768P choice belongs on the model widget, not
# on `size` (which only carries a pixel size or an aspect ratio).
# ---------------------------------------------------------------------------
MODEL_AUTO_2K = "auto · prefer 2K"
MODEL_AUTO_768P = "auto · prefer 768P"
MODEL_LITERAL_2K = "minimax-h3-2k"
MODEL_LITERAL_768P = "minimax-h3-768p"
MODEL_OFFICIAL = "MiniMax-H3"
MODEL_CUSTOM = "custom…"

MODEL_OPTIONS = [
    MODEL_AUTO_2K,
    MODEL_AUTO_768P,
    MODEL_LITERAL_2K,
    MODEL_LITERAL_768P,
    MODEL_OFFICIAL,
    MODEL_CUSTOM,
]
DEFAULT_MODEL = MODEL_AUTO_2K

# Used when the catalogue can't be read or holds no H3 entry.
FALLBACK_MODEL = MODEL_LITERAL_768P

# ---------------------------------------------------------------------------
# Size / aspect ratio
# ---------------------------------------------------------------------------
SIZE_AUTO = "auto · relay default"
SIZE_OPTIONS = [
    SIZE_AUTO,
    "16:9",
    "9:16",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
    "1344x768 (delivery 16:9)",
    "768x1344 (delivery 9:16)",
    "1280x720 (768P)",
    "720x1280 (768P)",
    "1024x1024 (768P)",
]
DEFAULT_SIZE = "16:9"

# --- Self-hosted gateway profile ---------------------------------------------
# The relay defaults below are EvoH3's. A local h3-gateway (the DSH box)
# differs in three ways that matter to these widgets; both are configurable so
# the same pack still works against the hosted relay.
#
# 1. Long clips. With --enable-triton-backend the comfy-kitchen int8 Triton
#    kernel silently diverges into full-frame noise past ~10s of frames (clean
#    at 240 frames, garbage at 362). A gateway running Triton sets
#    H3_MAX_SECONDS=10 and rejects longer jobs with HTTP 400, which surfaces as
#    a validation error mid-run. Clamping the slider up front is friendlier.
# 2. Delivery tiers. The gateway's Ref2VA default is the DMAD 4-step distilled
#    LoRA with lcm + shift_audio=2.0 and no cache; steps>4 is clamped back to 4.
#    8-step turbo belongs to FL2VA and is not what a bare submit gets.
# 3. Upscale. `upscale=WxH` is a plain form field, so it can be exposed as a
#    node input instead of requiring curl.
DURATION_MIN = 4
# The gateway refuses anything above H3_MAX_SECONDS (currently 10, because the
# Triton int8 kernel diverges past ~240 frames). Default the slider to that same
# cap so a fresh install cannot produce a 400; raise it here or via the env var
# if the gateway is redeployed without Triton.
DURATION_MAX = int(os.environ.get("EVOH3_MAX_SECONDS", "10"))
# The 10 s ceiling is a SINGLE-PASS limit (int8 diverges past ~243 frames). In
# long mode `duration` means the TOTAL length, which is bounded only by H3's
# frame grid (16376 frames ~= 11.3 min). One widget serves both, so the slider
# has to be generous and the single-pass cap is enforced at submit time instead.
# The chunked path is bounded by the plugin's `total_frames` grid, not by taste:
# H3Parameter caps total_frames at 16376 and fps is 24, i.e. 682.3 s of finished
# video. Anything longer is rejected by ComfyUI validation with a cryptic error.
TOTAL_SECONDS_CEILING = int(os.environ.get("EVOH3_MAX_TOTAL_SECONDS", "682"))
STEPS_DEFAULT = int(os.environ.get("EVOH3_STEPS", "4"))
# The node states a sharpness only; orientation AND aspect ratio come from `size`.
# A 768x1344 job asked for "1080p" gets 1080x1920, and the gateway rejects an
# explicit canvas whose aspect does not match `size` (the upscaler resizes
# unconditionally and would otherwise stretch the clip).
UPSCALE_OPTIONS = [
    "off",
    "1080p · ~+40s",
    "1440p",
]
UPSCALE_DEFAULT = "off"

# --- 长视频（AutoContext）--------------------------------------------------
# A long request is rendered by the gateway as sequential chunks; only the
# per-chunk length has to respect the int8 divergence ceiling (~10s), so the
# total duration becomes free.
LONG_MODE_OFF = "off"
LONG_MODE_AUTOCONTEXT = "autocontext · 分段拼接长视频"
LONG_MODE_OPTIONS = [LONG_MODE_OFF, LONG_MODE_AUTOCONTEXT]

CLIP_MODE_GLOBAL = "global"
CLIP_MODE_OPTIONS = [CLIP_MODE_GLOBAL, "Clip_Tag", "timeline"]

CHUNK_SECONDS_MIN = 4
CHUNK_SECONDS_MAX = 10          # single-pass int8 safety ceiling
CHUNK_SECONDS_DEFAULT = 8
CONTEXT_FRAMES_DEFAULT = 22     # 17n+5 grid; >=22 keeps seams stable

# In long mode the `duration` widget counts SEGMENTS, not seconds: one segment is
# `chunk_seconds` long, so the arithmetic is a single multiply. The widget max has
# to cover the widest case (4 s chunks -> 682/4 = 170 segments); the JS retargets
# it per mode and per chunk_seconds anyway.
SEGMENTS_MAX = int(os.environ.get("EVOH3_SEGMENTS_MAX",
                                  str(TOTAL_SECONDS_CEILING // CHUNK_SECONDS_MIN)))

# How the reference audio is handed to each chunk. This single switch decides
# whether the bound voice survives past the first segment, so it is a first-class
# widget rather than a hidden gateway default.
#   global     every chunk receives the FULL reference audio -> timbre holds
#   segmented  the reference is sliced by segment (face replacement / lip sync
#              against a real timeline); a 7s clip becomes sub-second fragments
REF_SYNC_GLOBAL = "global · 每段都用完整参考（锁音色，默认）"
REF_SYNC_SEGMENTED = "segmented · 按段切片参考（换脸对口型用）"
REF_SYNC_OPTIONS = [REF_SYNC_GLOBAL, REF_SYNC_SEGMENTED]
REF_SYNC_DEFAULT = REF_SYNC_GLOBAL

# Attention sparsity. Long renders default to the measured 2.16x budget on the
# gateway side; this widget only exists so the default can be overridden.
SPARSE_OPTIONS = [
    "auto · 长视频用 sparse 0.3（推荐）",
    "0.3 · 显式指定 2.16x 加速",
    "0.5 · 更激进，更快",
    "off · 不用 sparse（基准画质对照）",
]
SPARSE_DEFAULT = SPARSE_OPTIONS[0]


def _sparse_wire(label: str) -> str | None:
    """Map the accel widget to a `sparse` wire value; None means "omit".

    Omitting lets the gateway apply its per-mode default (0.3 for autocontext,
    off for single-pass), which is why "auto" is the default here.
    """
    label = (label or "").strip()
    if not label or label.startswith("auto"):
        return None
    if label.startswith("off"):
        return "0"
    return label.split(" ", 1)[0]


def _ref_sync_wire(label: str) -> str:
    """Map the ref-sync widget label back to the wire value."""
    label = (label or "").strip()
    if label.startswith("segmented"):
        return "segmented"
    return "global"

# Quality tiers exposed as a single widget instead of a LoRA filename. The
# gateway's own default (omit the field) is the DMAD delivery tier, which is the
# right choice unless the user is iterating on a prompt.
# JS lives in ./web/js; it retargets the `duration` slider when long_mode flips.
WEB_DIRECTORY = "./web/js"

QUALITY_OPTIONS = [
    "delivery · DMAD 4-step (recommended)",
    "draft · ref2v turbo 4-step (faster)",
    "raw · no LoRA, 8-step turbo default",
]
QUALITY_DEFAULT = QUALITY_OPTIONS[0]
# Draft LoRAs are TRAINED PER MODE: a ref2v LoRA on the FL2VA DiT (or vice
# versa) loads with zero matching keys and drifts the image — the original v4
# bug. The node picks the right file from what the user connected; the gateway
# cross-checks and rejects mismatches outright.
QUALITY_LORA_REF2V = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
QUALITY_LORA_FL2V = "minimax_h3_fl2v_turbo_4step_v1.2_768p_comfyui_bf16.safetensors"


def _resolve_base_url(value: str | None) -> str:
    """Normalize the gateway URL and reject the shipped placeholder.

    The endpoint is issued per deployment, so an unedited default must fail with an
    instruction rather than a confusing DNS/401 error several seconds into a submit.
    """
    url = (value or "").strip().rstrip("/") or DEFAULT_BASE_URL
    if any(h in url for h in PLACEHOLDER_HOSTS):
        raise ValueError(
            "请把 base_url 改成服务方分配给你的网关地址（例如 https://h3.example.net），"
            "不要保留示例占位符。地址和密钥由网关运营方提供。"
        )
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"base_url 必须以 http:// 或 https:// 开头，当前是 {url!r}")
    return url


def _quality_lora(label: str, has_refs: bool) -> str | None:
    """Map the quality tier to a `lora` wire value; None means "omit the field".

    Omitting lets the gateway apply its own default, which is the DMAD delivery
    tier for Ref2VA and the 8-step turbo for FL2VA.
    """
    label = (label or "").strip()
    if label == QUALITY_OPTIONS[1]:
        return QUALITY_LORA_REF2V if has_refs else QUALITY_LORA_FL2V
    if label == QUALITY_OPTIONS[2]:
        return "none"
    return None

# Two submit entries exist on this relay. The flat one is the only one that
# accepts local files (multipart `input_reference`) but carries reference
# *images* only. The v2 native entry is the only one that carries reference
# *video* and *audio* — i.e. motion — but it needs public URLs.
SUBMIT_PATH_FLAT = "/v1/videos"
SUBMIT_PATH_V2 = "/v2/video_generation"
QUERY_PATH_V2 = "/v2/query/video_generation"

# Custom wire type carrying the connection bundle to the worker nodes.
EvoH3KeyType = IO.Custom("EVOH3_KEY")

# The relay reports these, and its own docs map them to a coarse progress value
# (queued 20, generating 30, done 100).
STATUS_LABELS = {
    "queued": "Queued",
    "in_progress": "Generating",
    "running": "Generating",
    "completed": "Completed",
    "succeeded": "Completed",
    "success": "Completed",
    "failed": "Failed",
    "unknown": "Syncing",
}

# The relay's docs disagree with what it actually returns: the flat entry
# documents `completed`, while the v2 entry it proxies to uses `succeeded`.
# Accept the union rather than stalling on an unrecognised word.
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done", "finished"}
FAILED_STATUSES = {
    "failed",
    "fail",
    "failure",
    "cancelled",
    "canceled",
    "expired",
    "error",
    "rejected",
}

_H3_HINT = re.compile(r"h3", re.IGNORECASE)

# (base_url, key fingerprint) -> (fetched_at, [model ids])
_CATALOG: dict[str, tuple[float, list[str]]] = {}


class EvoH3Key:
    """Connection bundle produced by the key node and consumed by every worker."""

    def __init__(self, base_url: str, api_key: str, model: str, model_custom: str = ""):
        self.base_url = _resolve_base_url(base_url)
        self.api_key = api_key
        self.model = (model or "").strip() or DEFAULT_MODEL
        self.model_custom = (model_custom or "").strip()


def _auth_headers(key: EvoH3Key) -> dict[str, str]:
    if key is None or not key.api_key:
        raise ValueError(
            "EvoH3 API key is missing. Fill in the key node, or set "
            f"{KEY_ENV} in the environment ComfyUI was started from."
        )
    return {"Authorization": f"Bearer {key.api_key}"}


def _flag(value) -> bool:
    """Multipart form fields arrive as strings; JSON wants a real bool."""
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "on"}


def _size_value(label: str) -> str | None:
    """Map a labelled size option back to its wire value.

    ``None`` means "omit the field" so the relay applies its own default.
    """
    label = (label or "").strip()
    if not label or label == SIZE_AUTO:
        return None
    return label.split(" ", 1)[0]


def _resolution_of(model_id: str) -> str:
    """The `resolution` field the v2 entry expects, derived from the model id."""
    return "2K" if "2k" in (model_id or "").lower() else "768P"


def _url_list(text: str, limit: int, label: str) -> list[str]:
    """Split a multi-line URL box, ignoring blanks, enforcing a cap."""
    urls = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(urls) > limit:
        raise ValueError(f"At most {limit} {label} are supported, got {len(urls)}.")
    return urls


def _video_url_of(data: dict) -> str | None:
    """Pull the finished clip's URL out of either response shape."""
    flat = (data.get("metadata") or {}).get("url")
    if flat:
        return flat
    # v2-native fallback: { "task": { "content": { "url": ... } } }
    task = data.get("task") or {}
    return ((task.get("content") or {}).get("url")) or None


async def _download_with_key(key: "EvoH3Key", task_id: str) -> "VideoFromFile":
    """Fetch the finished clip with the caller's own key.

    The gateway no longer hands out pre-signed URLs: a link that downloads
    without a credential leaks the clip the moment it is pasted anywhere. So the
    bytes are pulled here, with an Authorization header, and wrapped in a
    VideoFromFile backed by memory.
    """
    url = f"{key.base_url}{SUBMIT_PATH_FLAT}/{task_id}/content"
    timeout = aiohttp.ClientTimeout(total=900, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url, headers=_auth_headers(key)) as resp:
            if resp.status == 404:
                raise RuntimeError(
                    f"Download refused (HTTP 404). The clip belongs to another account, "
                    f"or it has already expired (H3_RETENTION_HOURS)."
                )
            if resp.status == 401:
                raise RuntimeError(
                    "Download refused (HTTP 401): the API key is missing or revoked."
                )
            if resp.status == 410:
                raise RuntimeError(
                    "Download refused (HTTP 410): the file was already downloaded once "
                    "and deleted (H3_DELETE_AFTER_DOWNLOAD=1), or it expired."
                )
            if resp.status >= 400:
                raise RuntimeError(f"Download failed (HTTP {resp.status}): {(await resp.text())[:300]}")
            buffer = BytesIO(await resp.read())
    buffer.seek(0)
    return VideoFromFile(buffer)


def _show_status(node_cls, text: str, status: str | None = None) -> None:
    """Render the node's status line. Never let display problems fail a run."""
    if _display_text is None:
        return
    try:
        _display_text(node_cls, text, status=status)
    except Exception as exc:  # pragma: no cover - display only
        log.debug("status display failed: %s", exc)


# How long to keep asking for a free async task slot before giving up. The relay
# caps concurrent async tasks per account (2 by default) and a run cancelled on
# this side keeps occupying its slot until the upstream finishes, so a 429 is
# ordinary back-pressure rather than a failure.
SLOT_WAIT_TIMEOUT = 900.0
SLOT_WAIT_INTERVAL = 15.0


class EvoH3Busy(RuntimeError):
    """The relay refused a submit because the account is at its async task limit."""


async def _pause(node_cls, seconds: float, text: str, status: str | None = None) -> None:
    """Sleep in one-second slices: repaint the status, honour cancellation."""
    until = time.monotonic() + seconds
    while True:
        if is_processing_interrupted():
            raise ProcessingInterrupted()
        _show_status(node_cls, text, status=status)
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


async def _submit_waiting_for_slot(node_cls, submit):
    """Retry a submit while the relay reports the account task limit is reached.

    Without this the node fails instantly on a full account even though the
    slot frees up within a minute or two.
    """
    started = time.monotonic()
    announced = False
    while True:
        try:
            return await submit()
        except EvoH3Busy as exc:
            waited = time.monotonic() - started
            if waited >= SLOT_WAIT_TIMEOUT:
                raise RuntimeError(
                    f"EvoH3 refused the submit for {int(waited)}s because the account is "
                    f"at its async task limit; try again once the running tasks finish. {exc}"
                ) from exc
            if not announced:
                log.info("EvoH3 is at its async task limit; waiting for a free slot")
                announced = True
            await _pause(
                node_cls,
                SLOT_WAIT_INTERVAL,
                f"Waiting for a free task slot · {int(waited)}s",
                status="Account task limit",
            )


async def _list_models(key: EvoH3Key) -> list[str]:
    """Read the relay's model catalogue, cached briefly. [] when unreadable."""
    fingerprint = hashlib.sha256(f"{key.base_url}|{key.api_key}".encode()).hexdigest()
    now = time.monotonic()
    cached = _CATALOG.get(fingerprint)
    if cached is not None and now - cached[0] < CATALOG_TTL:
        return cached[1]

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(f"{key.base_url}/v1/models", headers=_auth_headers(key)) as resp:
                if resp.status >= 400:
                    log.warning("EvoH3 /v1/models returned HTTP %s", resp.status)
                    return []
                data = json.loads(await resp.text())
    except Exception as exc:
        log.warning("EvoH3 /v1/models lookup failed: %s", exc)
        return []

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    ids = [str(m["id"]) for m in items if isinstance(m, dict) and m.get("id")]
    if ids:
        _CATALOG[fingerprint] = (now, ids)
    return ids


def _pick_h3(ids: list[str], want: str | None) -> str | None:
    """Choose an H3 id: an explicit resolution hint wins, then 2K, then 768P."""
    h3 = [mid for mid in ids if _H3_HINT.search(mid)]
    if not h3:
        return None

    def rank(mid: str) -> tuple[int, int, str]:
        low = mid.lower()
        if want and want in low:
            return (0, len(mid), mid)
        if "2k" in low:
            return (1, len(mid), mid)
        if "768" in low or "720" in low:
            return (2, len(mid), mid)
        return (3, len(mid), mid)

    return sorted(h3, key=rank)[0]


async def _resolve_model(key: EvoH3Key) -> str:
    """Turn the model widget into a concrete relay model id.

    Auto modes consult the relay's catalogue, so a renamed model needs no code
    change. The earlier free-text values (`auto`, `auto:2k`, `auto:768p`) keep
    working, so a workflow saved against an older build doesn't break.
    """
    raw = (key.model or "").strip()
    low = raw.lower()

    if low.startswith("custom"):
        if not key.model_custom:
            raise ValueError("model is set to custom… but model_custom is empty.")
        return key.model_custom

    if not low.startswith("auto"):
        # A literal id — including MiniMax's own `MiniMax-H3` — passes through.
        return raw

    want = "768" if "768" in low else "2k"

    ids = await _list_models(key)
    if not ids:
        log.warning("EvoH3 model auto-detect found no catalogue; using %s", FALLBACK_MODEL)
        return FALLBACK_MODEL

    picked = _pick_h3(ids, want)
    if picked is None:
        log.warning("EvoH3 model auto-detect found no H3 entry in %s; using %s", ids, FALLBACK_MODEL)
        return FALLBACK_MODEL

    log.info("EvoH3 model auto-detect picked %r (hint=%r) from %s", picked, want, ids)
    return picked


async def _post_json(key: EvoH3Key, payload: dict, path: str = SUBMIT_PATH_FLAT) -> dict:
    url = f"{key.base_url}{path}"
    headers = {**_auth_headers(key), "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(url, headers=headers, data=json.dumps(payload)) as resp:
            text = await resp.text()
            if resp.status == 429:
                raise EvoH3Busy(f"HTTP 429: {text[:300]}")
            if resp.status >= 400:
                raise RuntimeError(f"EvoH3 submit failed (HTTP {resp.status}): {text[:900]}")
            return json.loads(text)


async def _post_multipart(key: EvoH3Key, fields: dict, file_bytes: bytes, filename: str) -> dict:
    """Submit with a real file part.

    This is the only local-media path EvoH3 supports, and it maps to the
    first frame (``input_reference``). Extra scalar fields ride along as form
    fields.
    """
    url = f"{key.base_url}/v1/videos"
    form = aiohttp.FormData()
    for name, value in fields.items():
        if value is None or value == "":
            continue
        form.add_field(name, str(value))
    form.add_field(
        "input_reference",
        BytesIO(file_bytes),
        filename=filename or "input_reference.png",
        content_type="application/octet-stream",
    )
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(url, headers=_auth_headers(key), data=form) as resp:
            text = await resp.text()
            if resp.status == 429:
                raise EvoH3Busy(f"HTTP 429: {text[:300]}")
            if resp.status >= 400:
                raise RuntimeError(f"EvoH3 submit failed (HTTP {resp.status}): {text[:900]}")
            return json.loads(text)


async def _cancel(key: EvoH3Key, task_id: str) -> None:
    """Best-effort upstream cancel when the local run is interrupted.

    The relay used to keep generating (and billing) after a local cancel. A
    gateway that implements ``POST /v1/videos/{id}/cancel`` can stop the GPU
    job; older relays simply return 404 and the original task keeps running.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            await sess.post(f"{key.base_url}/v1/videos/{task_id}/cancel",
                            headers=_auth_headers(key))
    except Exception as exc:  # noqa: BLE001 - cancel is best effort
        log.debug("EvoH3 cancel for %s failed: %s", task_id, exc)


async def _poll(node_cls, key: EvoH3Key, task_id: str, style: str = "flat") -> str:
    """Poll until the task completes, keeping the node's status line live.

    ``style`` selects the response shape: the flat entry reports ``status`` and
    ``metadata.url`` at the top level, while the v2 native entry nests them
    under ``task``.

    The status line is repainted from the wait loop itself rather than from a
    background task. Cancelling such a task re-raises ``CancelledError`` from
    the cleanup path, and ``CancelledError`` is a ``BaseException`` that
    ``contextlib.suppress(Exception)`` does not catch — the first version of
    this pack crashed the whole execution that way.
    """
    started = time.monotonic()
    deadline = started + POLL_TIMEOUT
    if style == "v2":
        url = f"{key.base_url}{QUERY_PATH_V2}/{task_id}"
    else:
        url = f"{key.base_url}{SUBMIT_PATH_FLAT}/{task_id}"
    headers = _auth_headers(key)
    timeout = aiohttp.ClientTimeout(total=120)

    bar = ProgressBar(100)
    latest = {"status": "queued"}

    async with aiohttp.ClientSession(timeout=timeout) as sess:
        while time.monotonic() < deadline:
            if is_processing_interrupted():
                raise ProcessingInterrupted()

            async with sess.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"EvoH3 poll failed (HTTP {resp.status}): {text[:900]}")
                data = json.loads(text)

            task_obj = data.get("task") or {}
            raw_status = task_obj.get("status") if style == "v2" else data.get("status")
            status = str(raw_status or "").lower()
            if status != latest["status"]:
                # Always leave a breadcrumb: an unrecognised status word is
                # exactly the failure this line exists to make obvious.
                log.info("EvoH3 task %s status=%r progress=%r", task_id, status, data.get("progress"))
            latest["status"] = status

            progress = data.get("progress")
            if isinstance(progress, (int, float)) and not isinstance(progress, bool):
                bar.update_absolute(max(0, min(100, int(progress))), total=100)

            if status in SUCCESS_STATUSES:
                # Completion no longer carries a download URL; the caller fetches
                # /content itself with the Authorization header.
                log.info("EvoH3 task %s finished (%s)", task_id, status)
                return task_id

            if status in FAILED_STATUSES:
                task = data.get("task") or {}
                err = data.get("error") or task.get("error") or {}
                raise RuntimeError(
                    f"EvoH3 task {task_id} failed: {err.get('message', status)} "
                    f"(code {err.get('code', 'unknown')})"
                )

            await _pause(
                node_cls,
                POLL_INTERVAL,
                f"Time elapsed: {int(time.monotonic() - started)}s",
                status=STATUS_LABELS.get(latest["status"], latest["status"]),
            )

    raise RuntimeError(f"EvoH3 task {task_id} timed out after {POLL_TIMEOUT:.0f}s")


async def _run(node_cls, key: EvoH3Key, payload: dict = None, fields: dict = None,
               image_bytes: bytes = None, filename: str = None,
               path: str = SUBMIT_PATH_FLAT, poll_style: str = "flat") -> IO.NodeOutput:
    # Resolve the model once, here, so every worker shares auto-detection.
    model = await _resolve_model(key)
    _show_status(node_cls, "Submitting to Evo…", status="Submitting")

    if image_bytes is not None:
        body = {**(fields or {}), "model": model}

        def submit():
            return _post_multipart(key, body, image_bytes, filename)
    else:
        body = {**(payload or {}), "model": model}

        def submit():
            return _post_json(key, body, path)

    submitted = await _submit_waiting_for_slot(node_cls, submit)

    task_id = submitted.get("task_id") or submitted.get("id")
    if not task_id:
        raise RuntimeError(f"Evo returned no task_id: {submitted}")

    try:
        await _poll(node_cls, key, task_id, poll_style)
    except ProcessingInterrupted:
        # Local cancel only stops this node; tell the upstream to stop too.
        await _cancel(key, task_id)
        raise
    _show_status(node_cls, "Downloading result…", status="Downloading")
    return await _download_with_key(key, task_id)


def _require_prompt(prompt: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required.")
    return prompt


def _size_fields(size_label: str) -> dict:
    """`size` is omitted entirely when the widget says auto."""
    value = _size_value(size_label)
    return {"size": value} if value else {}


def _upscale_value(label: str) -> str | None:
    """Map the upscale combo back to a wire value; None means "omit".

    The options are tiers ("1080p"), not canvases: the gateway derives both the
    orientation and the aspect ratio from `size`. Return the tier verbatim.
    """
    label = (label or "").strip()
    if not label or label.startswith("off"):
        return None
    return label.split(" ", 1)[0]


class EvoH3KeyNode(IO.ComfyNode):
    """Holds the relay's base URL, bearer token and model selection."""

    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="EvoH3KeyNode",
            display_name="Evo H3 API Key",
            category=CATEGORY,
            description=(
                "Connection settings for a EvoH3 relay. Connect the output to any "
                "EvoH3 H3 worker node. Prefer the environment variable so the key "
                "never lands in a workflow JSON."
            ),
            inputs=[
                IO.String.Input(
                    "base_url",
                    default=DEFAULT_BASE_URL,
                    tooltip="Relay base URL without a trailing slash and without /v1.",
                ),
                IO.String.Input(
                    "api_key",
                    default="PONG",
                    tooltip=f"Bearer token. Leave empty to read {KEY_ENV} from the environment.",
                ),
                IO.Combo.Input(
                    "model",
                    options=MODEL_OPTIONS,
                    default=DEFAULT_MODEL,
                    tooltip=(
                        "Resolution lives in the model id on this relay. The auto modes ask "
                        "the relay for its catalogue and match the wanted resolution; the "
                        "other entries are sent verbatim (switch to custom… for any id)."
                    ),
                ),
                IO.String.Input(
                    "model_custom",
                    default="",
                    advanced=True,
                    tooltip="Exact model id, used only when model is set to custom…",
                ),
            ],
            outputs=[EvoH3KeyType.Output()],
        )

    @classmethod
    def execute(cls, base_url, api_key, model, model_custom) -> IO.NodeOutput:
        resolved = ((api_key or "").strip()
                    or os.environ.get(KEY_ENV, "").strip()
                    or os.environ.get(LEGACY_KEY_ENV, "").strip())
        if not resolved:
            raise ValueError(
                f"EvoH3 API key is required. Fill in the api_key field, or set {KEY_ENV} "
                "in the environment ComfyUI was started from."
            )
        return IO.NodeOutput(EvoH3Key(base_url, resolved, model, model_custom))


class EvoH3FirstLastFrameNode(IO.ComfyNode):
    """First/last-frame to video.

    A connected ``first_frame`` tensor is uploaded as multipart
    ``input_reference`` — the only route that works on this relay, because
    ``/v1/files/upload`` is not implemented. Without a tensor, supply a public
    URL instead and the node uses the JSON form.
    """

    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="EvoH3FirstLastFrameNode",
            display_name="Evo H3 First/Last Frame to Video",
            category=CATEGORY,
            description=(
                "[旧节点] Animate from a first frame, with an "
                "optional last frame. Connect a local image to first_frame, or give "
                "a URL when no image is connected."
            ),
            inputs=[
                EvoH3KeyType.Input("key"),
                IO.String.Input("prompt", multiline=True, default="",
                                tooltip="Text description of the motion."),
                IO.String.Input("first_frame_url", default="",
                                tooltip="Public image URL, used only when no first_frame image is connected."),
                IO.String.Input("last_frame_url", default="", advanced=True,
                                tooltip="Optional public URL of the last frame."),
                IO.Combo.Input("size", options=SIZE_OPTIONS, default=DEFAULT_SIZE,
                               tooltip="Ignored by the relay when a first frame is supplied (ratio follows the image)."),
                IO.Int.Input("duration", default=4, min=DURATION_MIN, max=SEGMENTS_MAX, step=1,
                             display_mode=IO.NumberDisplay.slider,
                             tooltip=f"短视频模式：单条秒数（上限 {DURATION_MAX}s）。"
                                       f"长视频模式（long_mode=autocontext）：这里是【段数】，"
                                       f"总时长 = 段数 × chunk_seconds，上限 {TOTAL_SECONDS_CEILING}s。"),
                IO.Boolean.Input("watermark", default=False, advanced=True,
                                 tooltip="Ask the relay to add an AIGC watermark. "
                                         "NOT supported by the self-hosted h3-gateway: "
                                         "it returns 400 rather than pretend."),
                IO.Combo.Input("long_mode", options=LONG_MODE_OPTIONS,
                               default=LONG_MODE_OFF,
                               tooltip="off = 单条生成（≤10s）。"
                                       "autocontext = 分段拼接长视频：duration 变成【总时长】，"
                                       "首帧只在第一段生效，后面靠段间锚定帧续接。"
                                       "注意：长视频的人物一致性靠参考图锚定，"
                                       "只有首帧时人物会随段落变长而漂移，重要项目请改用 "
                                       "Evo H3 Reference Images / Reference + Voice。"),
                IO.Int.Input("chunk_seconds", default=CHUNK_SECONDS_DEFAULT,
                             min=CHUNK_SECONDS_MIN, max=CHUNK_SECONDS_MAX, step=1,
                             advanced=True,
                             tooltip="长视频模式下每段的秒数，必须 ≤10。"),
                IO.Int.Input("context_frames", default=CONTEXT_FRAMES_DEFAULT,
                             min=5, max=90, step=1, advanced=True,
                             tooltip="段间重叠锚定帧数，22 是推荐起点。"),
                IO.Combo.Input("clip_mode", options=CLIP_MODE_OPTIONS,
                               default=CLIP_MODE_GLOBAL, advanced=True,
                               tooltip="global = 同一段提示词套用到所有分段。"),
                IO.Combo.Input("accel", options=SPARSE_OPTIONS, default=SPARSE_DEFAULT,
                               advanced=True,
                               tooltip="长视频注意力稀疏加速。auto = 网关按模式决定。"),
                IO.Image.Input("first_frame", optional=True,
                               tooltip="Local first frame. Uploaded as multipart input_reference."),
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(cls, key, prompt, first_frame_url, last_frame_url, size, duration,
                      watermark, long_mode=LONG_MODE_OFF,
                      chunk_seconds=CHUNK_SECONDS_DEFAULT,
                      context_frames=CONTEXT_FRAMES_DEFAULT,
                      clip_mode=CLIP_MODE_GLOBAL, accel=SPARSE_DEFAULT,
                      first_frame=None) -> IO.NodeOutput:
        prompt = _require_prompt(prompt)
        first_frame_url = (first_frame_url or "").strip()
        last_frame_url = (last_frame_url or "").strip()
        if first_frame is None and not first_frame_url:
            raise ValueError("Connect a first_frame image or fill in first_frame_url.")

        base = {
            "prompt": prompt,
            "duration": int(duration),
            **_size_fields(size),
        }
        # Long-mode fields ride along as ordinary scalars on BOTH submit paths:
        # the multipart entry only special-cases the image part, everything else
        # arrives as a form field, which the gateway parses identically.
        _apply_long_mode(base, long_mode=long_mode, chunk_seconds=chunk_seconds,
                         context_frames=context_frames, clip_mode=clip_mode,
                         duration_units=duration, sparse=accel)

        if first_frame is not None:
            fields = dict(base)
            if _flag(watermark):
                fields["aigc_watermark"] = "true"
            if last_frame_url:
                fields["last_frame_image"] = last_frame_url
            buffer = tensor_to_bytesio(first_frame)
            return IO.NodeOutput(await _run(
                cls,
                key,
                fields=fields,
                image_bytes=buffer.getvalue(),
                filename=getattr(buffer, "name", None) or "first_frame.png",
            ))

        payload = dict(base)
        payload["first_frame_image"] = first_frame_url
        if last_frame_url:
            payload["last_frame_image"] = last_frame_url
        if _flag(watermark):
            payload["aigc_watermark"] = True
        return IO.NodeOutput(await _run(cls, key, payload=payload))


async def _upload_bytes(key: EvoH3Key, data: bytes, filename: str, content_type: str) -> str:
    """Upload one local asset to the gateway, return its file_… id."""
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=content_type)
    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(f"{key.base_url}/v1/files",
                             headers=_auth_headers(key), data=form) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"EvoH3 file upload failed (HTTP {resp.status}): {text[:600]}")
            data = json.loads(text)
            file_id = data.get("id")
            if not file_id:
                raise RuntimeError(f"EvoH3 file upload returned no id: {text[:600]}")
            return file_id


def _image_bytes(image) -> tuple[bytes, str]:
    buf = tensor_to_bytesio(image)
    return buf.getvalue(), getattr(buf, "name", None) or "reference.png"


def _audio_wav_bytes(audio) -> tuple[bytes, str]:
    """ComfyUI AUDIO dict {waveform:[B,C,N], sample_rate:int} -> 16-bit WAV."""
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    if waveform.ndim == 3:
        waveform = waveform[0]
    waveform = waveform.detach().cpu().float().clamp(-1.0, 1.0)
    channels = int(waveform.shape[0])
    pcm = (waveform.transpose(0, 1).numpy() * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue(), "reference_audio.wav"


def _video_mp4_bytes(video) -> tuple[bytes, str]:
    """Save a ComfyUI VIDEO to a temporary MP4 and return its bytes."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        try:
            video.save_to(path, format="mp4", codec="h264")
        except Exception:
            video.save_to(path)
        with open(path, "rb") as f:
            return f.read(), "reference_video.mp4"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Shared request plumbing (every generation node uses these three helpers)
# ---------------------------------------------------------------------------

def _base_payload(prompt: str, duration, size: str, seed, steps) -> dict:
    """The field set every submit entry accepts.

    `model` is deliberately absent: `_run()` resolves it once and injects it, so
    auto-detection stays consistent across every node.
    """
    return {
        "prompt": prompt, "seconds": int(duration),
        "size": size, "seed": int(seed), "steps": int(steps),
    }


def _apply_tuning_fields(payload: dict, *, upscale, quality,
                         has_refs: bool = True) -> None:
    """Add optional quality/upscale knobs, omitting them when at defaults."""
    upscale_value = _upscale_value(upscale)
    if upscale_value:
        payload["upscale"] = upscale_value
    lora_value = _quality_lora(quality, has_refs)
    if lora_value:
        payload["lora"] = lora_value


def _check_duration(duration, long_mode) -> None:
    """Single passes are capped at DURATION_MAX; long mode reinterprets duration.

    The widget range has to cover a 10 minute total, so the 10 s single-pass
    ceiling can no longer live in the input schema. Enforce it here with an
    actionable message instead of letting the gateway answer 400.
    """
    if long_mode and long_mode != LONG_MODE_OFF:
        return
    if int(duration) > DURATION_MAX:
        raise ValueError(
            f"duration={int(duration)} 超过单次生成上限 {DURATION_MAX} 秒。"
            "int8 算子在 10 秒以后会静默发散成彩色噪点，所以网关会直接拒。"
            "要更长请把 long_mode 设为 autocontext —— 那时 duration 表示【总时长】。"
        )


def _apply_long_mode(payload: dict, *, long_mode, chunk_seconds,
                     context_frames, clip_mode, duration_units,
                     ref_sync_mode=REF_SYNC_DEFAULT, sparse=SPARSE_DEFAULT) -> None:
    """Turn the `duration` widget into a segment count and enable chunked rendering.

    Short mode: `duration_units` is SECONDS and must stay within DURATION_MAX.
    Long mode : `duration_units` is a NUMBER OF SEGMENTS; one segment renders
                `chunk_seconds` of video, so total = segments * chunk_seconds.

    Mutates `payload` in place, rewriting its length field to the computed total
    seconds so the gateway keeps receiving plain seconds.
    """
    units = int(duration_units or 0)
    if not long_mode or long_mode == LONG_MODE_OFF:
        _check_duration(units, LONG_MODE_OFF)
        return

    chunk = int(chunk_seconds)
    if chunk > CHUNK_SECONDS_MAX:
        raise ValueError(
            f"chunk_seconds={chunk} 超过单段安全上限 {CHUNK_SECONDS_MAX} 秒。"
            "int8 算子在 >10s 的长序列上会发散出彩色噪点（实测 15s 从 10s 起全屏雪花），"
            "请把 chunk_seconds 调回 4–10。"
        )
    if chunk < CHUNK_SECONDS_MIN:
        raise ValueError(
            f"chunk_seconds={chunk} 太小，最小 {CHUNK_SECONDS_MIN} 秒（再短会导致段数暴增）。"
        )
    ctx = int(context_frames)
    if ctx < 5:
        raise ValueError(f"context_frames={ctx} 太小，最小 5。")
    if units < 1:
        raise ValueError("长视频模式下 duration 表示【段数】，至少 1 段。")

    total = units * chunk
    if total > TOTAL_SECONDS_CEILING:
        raise ValueError(
            f"{units} 段 × {chunk} 秒 = {total} 秒，超过长视频上限 {TOTAL_SECONDS_CEILING} 秒"
            f"（≈{TOTAL_SECONDS_CEILING // 60} 分 {TOTAL_SECONDS_CEILING % 60} 秒）。"
            f"在 chunk_seconds={chunk} 时最多 {TOTAL_SECONDS_CEILING // chunk} 段。"
            "上限来自 AutoContext 的 total_frames 最大 16376 帧 ÷ 24fps。"
        )

    if "seconds" in payload:
        payload["seconds"] = total
    elif "duration" in payload:
        payload["duration"] = total
    payload["mode"] = "autocontext"
    payload["chunk_seconds"] = chunk
    payload["context_frames"] = ctx
    payload["clip_mode"] = str(clip_mode)
    payload["ref_sync_mode"] = _ref_sync_wire(ref_sync_mode)
    sparse_wire = _sparse_wire(sparse)
    if sparse_wire is not None:
        payload["sparse"] = sparse_wire


class EvoH3ReferenceImagesNode(IO.ComfyNode):
    """多图参考专用节点 —— 最多 9 张参考图，不接受音频/视频。

    与 `Evo H3 Reference + Voice` 物理隔离：这里没有音频槽位，
    所以不存在「接了音频导致图上限从 9 降到 3」的隐式降级。
    想要音色锁定请用另一个节点。
    """

    @classmethod
    def define_schema(cls):
        image_inputs = [
            IO.Image.Input(
                f"ref_image_{i}", optional=True,
                tooltip=f"参考图 {i + 1}/9。prompt 里用 <Picture {i + 1}> 引用。"
                        + ("多角度覆盖比同角度多张更有效（正面/3-4侧面/全身）。" if i == 0 else ""),
            )
            for i in range(9)
        ]
        return IO.Schema(
            node_id="EvoH3ReferenceImagesNode",
            display_name="Evo H3 Reference Images (max 9)",
            category=CATEGORY,
            description=(
                "纯多图参考模式：最多 9 张图，无音频。"
                "适合锁人物形体、锁场景。"
                "需要锁音色请改用 Evo H3 Reference + Voice（上限 3 图）。"
            ),
            inputs=[
                EvoH3KeyType.Input("key"),
                IO.String.Input("prompt", multiline=True, default="",
                                tooltip="用 <Picture 1>…<Picture 9> 引用参考图。"),
                IO.Combo.Input("size", options=SIZE_OPTIONS, default=DEFAULT_SIZE),
                IO.Int.Input("duration", default=5, min=DURATION_MIN, max=SEGMENTS_MAX, step=1,
                             display_mode=IO.NumberDisplay.slider,
                             tooltip=f"短视频模式：单条秒数（上限 {DURATION_MAX}s）。"
                                       f"长视频模式：这里是【段数】，总时长 = 段数 × chunk_seconds。"),
                IO.Int.Input("seed", default=-1, min=-1, max=2**31 - 1, step=1, advanced=True,
                             tooltip="-1 = 随机。"),
                IO.Combo.Input("quality", options=QUALITY_OPTIONS, default=QUALITY_DEFAULT,
                               advanced=True,
                               tooltip="delivery = DMAD 4 步（网关默认）；draft = turbo 4 步；raw = 无 LoRA。"),
                IO.Int.Input("steps", default=STEPS_DEFAULT, min=1, max=50, step=1, advanced=True,
                             tooltip="4 = 交付默认（DMAD 蒸馏，网关会把 >4 钳回 4）。"),
                IO.Combo.Input("upscale", options=UPSCALE_OPTIONS, default=UPSCALE_DEFAULT,
                               advanced=True,
                               tooltip="采样后 3D latent 放大。只决定清晰度，方向由 size 决定。"),
                IO.Boolean.Input("watermark", default=False, advanced=True,
                                 tooltip="自建网关不支持，传 true 会 400。"),
                IO.Combo.Input("long_mode", options=LONG_MODE_OPTIONS,
                               default=LONG_MODE_OFF,
                               tooltip="off = 单条生成，时长受 int8 发散上限约束（≤10s）。"
                                       "autocontext = 分段拼接长视频：duration 变成【总时长】，"
                                       "按 chunk_seconds 切段、段间用 context_frames 锚定，"
                                       "总长可到几分钟。需要网关已部署 AutoContext。"),
                IO.Int.Input("chunk_seconds", default=CHUNK_SECONDS_DEFAULT,
                             min=CHUNK_SECONDS_MIN, max=CHUNK_SECONDS_MAX, step=1,
                             advanced=True,
                             tooltip="长视频模式下每段的秒数，必须 ≤10（int8 安全上限）。"
                                     "8 是实测最优（8s 段 + sparse 约 200s/段）。"),
                IO.Int.Input("context_frames", default=CONTEXT_FRAMES_DEFAULT,
                             min=5, max=90, step=1, advanced=True,
                             tooltip="段间重叠锚定帧数（17n+5 网格）。越大越连续，22 是推荐起点。"),
                IO.Combo.Input("clip_mode", options=CLIP_MODE_OPTIONS,
                               default=CLIP_MODE_GLOBAL, advanced=True,
                               tooltip="global = 同一段提示词套用到所有分段（省事，但镜头会在段边界漂移）；"
                                       "Clip_Tag = 用「段1」「段2」标签分段写，每段独立描述；"
                                       "timeline = 用「0-8s」时间标记分段。"
                                       "要一镜到底必须用后两种。"),
                IO.Combo.Input("accel", options=SPARSE_OPTIONS, default=SPARSE_DEFAULT,
                               advanced=True,
                               tooltip="长视频模式的注意力稀疏加速。auto = 由网关按模式决定"
                                       "（长视频默认 0.3，实测 2.16× 且锐度 +23.5%）；"
                                       "off = 走基准路径做画质对照。"),
                *image_inputs,
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(cls, key, prompt, size, duration, seed, steps, quality, upscale, watermark,
                      long_mode=LONG_MODE_OFF, chunk_seconds=CHUNK_SECONDS_DEFAULT,
                      context_frames=CONTEXT_FRAMES_DEFAULT, clip_mode=CLIP_MODE_GLOBAL,
                      accel=SPARSE_DEFAULT,
                      ref_image_0=None, ref_image_1=None, ref_image_2=None,
                      ref_image_3=None, ref_image_4=None, ref_image_5=None,
                      ref_image_6=None, ref_image_7=None, ref_image_8=None) -> IO.NodeOutput:
        prompt = _require_prompt(prompt)
        images = [img for img in (ref_image_0, ref_image_1, ref_image_2, ref_image_3,
                                  ref_image_4, ref_image_5, ref_image_6, ref_image_7,
                                  ref_image_8) if img is not None]
        if not images:
            raise ValueError("至少要接一张参考图。纯文生视频请用 Evo H3 Text to Video。")
        if len(images) > 9:
            raise ValueError(f"最多 9 张参考图，当前 {len(images)} 张。")

        _show_status(cls, "Uploading reference images…", status="Uploading")
        payload = _base_payload(prompt, duration, size, seed, steps)
        _apply_long_mode(payload, long_mode=long_mode, chunk_seconds=chunk_seconds,
                         context_frames=context_frames, clip_mode=clip_mode,
                         duration_units=duration, sparse=accel)
        _apply_tuning_fields(payload, upscale=upscale, quality=quality)

        payload["reference_images"] = [
            await _upload_bytes(key, *_image_bytes(image), "image/png")
            for image in images
        ]
        return IO.NodeOutput(await _run(cls, key, payload=payload))


class EvoH3ReferenceVoiceNode(IO.ComfyNode):
    """多图 + 音频 + 视频参考节点 —— 图上限 3 张，锁音色用这个。

    与 `Evo H3 Reference Images (max 9)` 物理隔离：只有 3 个图槽位，
    所以「图被静默截断」不可能发生。
    """

    @classmethod
    def define_schema(cls):
        image_inputs = [
            IO.Image.Input(
                f"ref_image_{i}", optional=True,
                tooltip=f"参考图 {i + 1}/3。motion 模式上限 3 张；"
                        "多角度覆盖比同角度多张更有效。",
            )
            for i in range(3)
        ]
        return IO.Schema(
            node_id="EvoH3ReferenceVoiceNode",
            display_name="Evo H3 Reference + Voice (3 img + audio)",
            category=CATEGORY,
            description=(
                "多模态参考模式：最多 3 张图 + 1 段音频 + 1 段视频。"
                "接 ref_audio 后走 motion 分支，语音音色由音频锚定。"
                "需要超过 3 张图请改用 Evo H3 Reference Images (max 9)。"
            ),
            inputs=[
                EvoH3KeyType.Input("key"),
                IO.String.Input("prompt", multiline=True, default="",
                                tooltip="用 <Picture 1>、<Video 1>、<Audio 1> 引用。"),
                IO.Combo.Input("size", options=SIZE_OPTIONS, default=DEFAULT_SIZE),
                IO.Int.Input("duration", default=5, min=DURATION_MIN, max=SEGMENTS_MAX, step=1,
                             display_mode=IO.NumberDisplay.slider,
                             tooltip=f"短视频模式：单条秒数（上限 {DURATION_MAX}s）。"
                                       f"长视频模式：这里是【段数】，总时长 = 段数 × chunk_seconds。"),
                IO.Int.Input("seed", default=-1, min=-1, max=2**31 - 1, step=1, advanced=True,
                             tooltip="-1 = 随机。"),
                IO.Combo.Input("quality", options=QUALITY_OPTIONS, default=QUALITY_DEFAULT,
                               advanced=True,
                               tooltip="delivery = DMAD 4 步；draft = turbo 4 步；raw = 无 LoRA。"),
                IO.Int.Input("steps", default=STEPS_DEFAULT, min=1, max=50, step=1, advanced=True,
                             tooltip="4 = 交付默认。"),
                IO.Combo.Input("upscale", options=UPSCALE_OPTIONS, default=UPSCALE_DEFAULT,
                               advanced=True,
                               tooltip="采样后 3D latent 放大。"),
                IO.Boolean.Input("watermark", default=False, advanced=True,
                                 tooltip="自建网关不支持，传 true 会 400。"),
                IO.Combo.Input("long_mode", options=LONG_MODE_OPTIONS,
                               default=LONG_MODE_OFF,
                               tooltip="off = 单条生成，时长受 int8 发散上限约束（≤10s）。"
                                       "autocontext = 分段拼接长视频：duration 变成【总时长】。"
                                       "本节点是长视频的主力节点——每段都会同时拿到同一组参考图"
                                       "（锁人物）和同一条参考音频（锁音色）。"),
                IO.Int.Input("chunk_seconds", default=CHUNK_SECONDS_DEFAULT,
                             min=CHUNK_SECONDS_MIN, max=CHUNK_SECONDS_MAX, step=1,
                             advanced=True,
                             tooltip="长视频模式下每段的秒数，必须 ≤10（int8 安全上限）。"
                                     "8 是实测最优。"),
                IO.Int.Input("context_frames", default=CONTEXT_FRAMES_DEFAULT,
                             min=5, max=90, step=1, advanced=True,
                             tooltip="段间重叠锚定帧数（17n+5 网格）。22 是推荐起点，"
                                     "担心接缝可以调到 39。"),
                IO.Combo.Input("clip_mode", options=CLIP_MODE_OPTIONS,
                               default=CLIP_MODE_GLOBAL, advanced=True,
                               tooltip="global = 整段提示词套用到所有分段；"
                                       "要一镜到底必须用 Clip_Tag 或 timeline 逐段写。"),
                IO.Combo.Input("ref_sync_mode", options=REF_SYNC_OPTIONS,
                               default=REF_SYNC_DEFAULT, advanced=True,
                               tooltip="参考音频怎么喂给每一段。"
                                       "global（默认）= 每段都用完整参考音频 → 音色跨段不漂移；"
                                       "segmented = 按段把参考音频切片 → 只适合「拿原片换脸对口型」，"
                                       "音色参考会被切成零点几秒的碎片，音色必然崩。"),
                IO.Combo.Input("accel", options=SPARSE_OPTIONS, default=SPARSE_DEFAULT,
                               advanced=True,
                               tooltip="长视频注意力稀疏加速。auto = 网关按模式决定（长视频默认 0.3）。"),
                *image_inputs,
                IO.Video.Input("ref_video", optional=True,
                               tooltip="参考视频帧；prompt 里用 <Video 1>。"),
                IO.Audio.Input("ref_video_audio", optional=True,
                               tooltip="与 ref_video 配套的音轨（可选）。"),
                IO.Audio.Input("ref_audio", optional=True,
                               tooltip="独立参考音频，用于锁定音色；prompt 里用 <Audio 1>。"
                                       "接入后本节点进入 motion 模式。"),
            ],
            outputs=[IO.Video.Output()],
        )

    @classmethod
    async def execute(cls, key, prompt, size, duration, seed, steps, quality, upscale, watermark,
                      long_mode=LONG_MODE_OFF, chunk_seconds=CHUNK_SECONDS_DEFAULT,
                      context_frames=CONTEXT_FRAMES_DEFAULT, clip_mode=CLIP_MODE_GLOBAL,
                      ref_sync_mode=REF_SYNC_DEFAULT, accel=SPARSE_DEFAULT,
                      ref_image_0=None, ref_image_1=None, ref_image_2=None,
                      ref_video=None, ref_video_audio=None, ref_audio=None) -> IO.NodeOutput:
        prompt = _require_prompt(prompt)
        images = [img for img in (ref_image_0, ref_image_1, ref_image_2) if img is not None]
        has_media = bool(images) or ref_video is not None
        if ref_audio is not None and not has_media:
            raise ValueError(
                "接了 ref_audio 但没有参考图或参考视频。"
                "音色需要绑定到一个形象上：请至少接一张 ref_image，或接一段 ref_video。"
            )
        if not has_media and ref_audio is None and ref_video_audio is None:
            raise ValueError(
                "本节点需要至少一张参考图、一段参考视频或一段参考音频。"
                "纯文生视频请用 Evo H3 Text to Video；纯多图请用 Evo H3 Reference Images (max 9)。"
            )

        _show_status(cls, "Uploading reference media…", status="Uploading")
        payload = _base_payload(prompt, duration, size, seed, steps)
        _apply_long_mode(payload, long_mode=long_mode, chunk_seconds=chunk_seconds,
                         context_frames=context_frames, clip_mode=clip_mode,
                         duration_units=duration, ref_sync_mode=ref_sync_mode,
                         sparse=accel)
        _apply_tuning_fields(payload, upscale=upscale, quality=quality)

        if images:
            payload["reference_images"] = [
                await _upload_bytes(key, *_image_bytes(image), "image/png")
                for image in images
            ]
        if ref_video is not None:
            payload["reference_videos"] = [
                await _upload_bytes(key, *_video_mp4_bytes(ref_video), "video/mp4")]
        audio_sources = [a for a in (ref_video_audio, ref_audio) if a is not None]
        if audio_sources:
            payload["reference_audios"] = [
                await _upload_bytes(key, *_audio_wav_bytes(audio), "audio/wav")
                for audio in audio_sources
            ]
        return IO.NodeOutput(await _run(cls, key, payload=payload))


class EvoH3Extension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[IO.ComfyNode]]:
        return [
            EvoH3KeyNode,
            EvoH3FirstLastFrameNode,
            EvoH3ReferenceImagesNode,
            EvoH3ReferenceVoiceNode,
        ]


async def comfy_entrypoint() -> EvoH3Extension:
    return EvoH3Extension()
