"""A small ASGI app exposing bibcheck over HTTP.

Deliberately dependency-free: a command-line tool should not grow a web
framework just to offer a demo. Three routes and a JSON body need no router.

The Python package is the single source of truth -- this module runs the *same*
checks the CLI runs, so the site can never drift from the tool.

Serverless realities this accounts for:

* **No persistent disk.** The cache lives under ``/tmp``, which survives only
  while an instance stays warm. Everything still works; re-runs are just less
  free than they are locally.
* **A hard execution deadline.** Work is bounded by an entry cap and a wall
  clock, and exceeding either returns a clear explanation rather than a
  platform timeout page.
* **Untrusted input.** Body size, entry count and manuscript size are all
  capped before any work begins.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Iterable, MutableMapping

from .config import Config, load_config
from .engine import check_bibliography
from .parsing import parse_bibtex

__all__ = ["create_app", "BibcheckApp"]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

#: Caps. Generous for a real bibliography, small enough to bound the work.
MAX_BODY_BYTES: Final[int] = 2 * 1024 * 1024
MAX_ENTRIES: Final[int] = 200
MAX_MANUSCRIPT_BYTES: Final[int] = 4 * 1024 * 1024
DEADLINE_SECONDS: Final[float] = 45.0

#: Distinguishes "caller did not specify a static root" (auto-detect) from
#: "caller explicitly passed None" (serve no static files).
_AUTODETECT: Final[Any] = object()

_JSON: Final[str] = "application/json; charset=utf-8"
_HTML: Final[str] = "text/html; charset=utf-8"


def _static_root() -> Path | None:
    """Locate the directory holding index.html.

    Checked in order so the app works from a source checkout, an installed
    package and a serverless bundle without any of them needing to agree.
    """
    override = os.environ.get("BIBCHECK_WEB_ROOT")
    candidates: list[Path] = [Path(override)] if override else []
    here = Path(__file__).resolve()
    candidates.extend(parent / "public" for parent in here.parents)
    candidates.append(Path.cwd() / "public")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _serverless_config() -> Config:
    """Config for a request handler.

    A serverless filesystem is read-only apart from ``/tmp``, so the cache goes
    there. It is still worth having: one warm instance serving several requests
    reuses it, and a bibliography citing the same work twice fetches once.
    """
    cache_dir = Path(os.environ.get("BIBCHECK_CACHE_DIR", "/tmp/bibcheck-cache"))
    return load_config(cache_dir=cache_dir)


class BibcheckApp:
    """The ASGI application."""

    def __init__(
        self,
        *,
        static_root: Path | None = _AUTODETECT,
        config: Config | None = None,
    ) -> None:
        self._static_root = _static_root() if static_root is _AUTODETECT else static_root
        self._config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()

        try:
            if path in ("/", "/index.html"):
                await self._serve_index(send, method)
            elif path == "/api/check" and method == "POST":
                await self._check(scope, receive, send)
            elif path == "/api/health":
                await _json(send, 200, {"status": "ok"})
            elif method not in ("GET", "POST"):
                await _json(send, 405, {"error": f"{method} is not allowed"})
            else:
                await _json(send, 404, {"error": "not found"})
        except Exception as error:  # noqa: BLE001 - a handler must never 500 silently
            await _json(
                send,
                500,
                {"error": "internal error", "detail": f"{type(error).__name__}: {error}"},
            )

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _serve_index(self, send: Send, method: str) -> None:
        if method != "GET":
            await _json(send, 405, {"error": f"{method} is not allowed"})
            return
        if self._static_root is None:
            await _respond(send, 500, _HTML, b"<h1>index.html was not found</h1>")
            return
        body = (self._static_root / "index.html").read_bytes()
        await _respond(send, 200, _HTML, body, extra={"cache-control": "no-cache"})

    async def _check(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw, too_large = await _read_body(receive, MAX_BODY_BYTES)
        if too_large:
            await _json(
                send,
                413,
                {"error": f"bibliography is larger than {MAX_BODY_BYTES // 1024} KB"},
            )
            return

        bib_text, tex_text, error = _parse_request(raw, scope)
        if error is not None:
            await _json(send, 400, {"error": error})
            return

        parsed = parse_bibtex(bib_text, path="uploaded.bib")
        if not parsed.entries and not parsed.problems:
            await _json(send, 400, {"error": "no BibTeX entries were found in that file"})
            return

        truncated = False
        if len(parsed.entries) > MAX_ENTRIES:
            from dataclasses import replace

            parsed = replace(parsed, entries=parsed.entries[:MAX_ENTRIES])
            truncated = True

        config = self._config or _serverless_config()
        try:
            report = await asyncio.wait_for(
                check_bibliography(parsed, config, manuscript_text=tex_text),
                timeout=DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            await _json(
                send,
                504,
                {
                    "error": (
                        f"checking took longer than {DEADLINE_SECONDS:.0f}s. "
                        "Try a smaller file, or run bibcheck locally where results "
                        "are cached between runs."
                    )
                },
            )
            return

        payload = report.to_json()
        payload["truncated"] = truncated
        if truncated:
            payload["notice"] = (
                f"only the first {MAX_ENTRIES} entries were checked; "
                "run bibcheck locally for a bibliography this size"
            )
        await _json(send, 200, payload)


def _parse_request(raw: bytes, scope: Scope) -> tuple[str, str | None, str | None]:
    """Accept either a JSON envelope or a raw .bib body.

    Returns ``(bib, tex, error)``.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    if not text.strip():
        return "", None, "the request body was empty"

    content_type = ""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-type":
            content_type = value.decode("latin-1", "replace").lower()
            break

    if "application/json" in content_type or text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return "", None, "the request body was not valid JSON"
        if not isinstance(payload, dict):
            return "", None, "expected a JSON object with a 'bib' field"
        bib = payload.get("bib")
        tex = payload.get("tex")
        if not isinstance(bib, str) or not bib.strip():
            return "", None, "expected a non-empty 'bib' field"
        if tex is not None and not isinstance(tex, str):
            return "", None, "'tex' must be a string if present"
        if tex is not None and len(tex.encode("utf-8")) > MAX_MANUSCRIPT_BYTES:
            return "", None, "the manuscript is too large"
        return bib, (tex or None), None

    return text, None, None


async def _read_body(receive: Receive, limit: int) -> tuple[bytes, bool]:
    """Read the request body, stopping as soon as the limit is exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        if chunk:
            total += len(chunk)
            if total > limit:
                return b"", True
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


async def _respond(
    send: Send,
    status: int,
    content_type: str,
    body: bytes,
    *,
    extra: dict[str, str] | None = None,
) -> None:
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        (b"x-content-type-options", b"nosniff"),
    ]
    for name, value in (extra or {}).items():
        headers.append((name.encode(), value.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send: Send, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await _respond(send, status, _JSON, body)


def create_app(**kwargs: Any) -> BibcheckApp:
    """Build the ASGI application."""
    return BibcheckApp(**kwargs)


#: Module-level instance, so ``bibcheck.web:app`` resolves for any ASGI server.
app = create_app()
