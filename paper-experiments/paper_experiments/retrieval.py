"""Native LeanSearch configuration, restricted transport, and immutable query replay."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

from .prepare import ROOT, load_manifest, sha256, write_json
from .protocol import content_hash

CONFIG = ROOT / "configs" / "leansearch.json"
API_SCHEMA = ROOT / "configs" / "leansearch-openapi.json"


def registered_settings(config_path: Path = CONFIG) -> dict[str, Any]:
    config = json.loads(config_path.read_text())
    if config["server_url"] != SERVER_URL:
        raise ValueError("Only the approved LeanSearch API may be used")
    if config["mathlib_release"] != "v4.28.0-rc1":
        raise ValueError("LeanSearch and experiment Mathlib releases must match")
    settings = {
        "tool_type": "search_lean_search",
        "server_url": config["server_url"],
        "max_results": 6,
        "timeout": 60,
        "max_retries": 3,
        "retry_delay": 2,
    }
    for key in ("max_results", "timeout", "max_retries", "retry_delay"):
        if config[key] != settings[key]:
            raise ValueError("Native LeanSearch settings differ from the registered defaults")
    return {"tool_config": settings, "mathlib_release": config["mathlib_release"]}


def prepare_native_retrieval(bundle: Path, archive: Path) -> dict[str, Any]:
    manifest = load_manifest(bundle)
    spec = json.loads((ROOT / "configs" / "putnam2025.json").read_text())
    settings = registered_settings()
    if (
        manifest["lean_toolchain"] != "leanprover/lean4:" + settings["mathlib_release"]
        or manifest["mathlib_revision"] != spec["mathlib_revision"]
    ):
        raise ValueError("LeanSearch and the experiment's pinned Lean/Mathlib versions differ")
    prefix = "dependencies/mathlib/"
    corpus = {
        name.removeprefix(prefix): digest
        for name, digest in manifest["files"].items()
        if name.startswith(prefix)
        and name.endswith(".lean")
        and (name == prefix + "Mathlib.lean" or name.startswith(prefix + "Mathlib/"))
    }
    if not corpus:
        raise ValueError("The prepared bundle has no pinned Mathlib sources")
    metadata = {
        "schema_version": 2,
        "adapter": "ax-prover-native-leansearch",
        **settings,
        "lean_toolchain": manifest["lean_toolchain"],
        "mathlib_revision": manifest["mathlib_revision"],
        "mathlib_corpus_sha256": content_hash(corpus),
        "api_schema_sha256": sha256(API_SCHEMA),
        "version_basis": "operator-confirmed public corpus release; API has no revision metadata",
        "network_policy": "host-mediated POST /search only; redirects forbidden; containers have no network",
    }
    archive.mkdir(parents=True, exist_ok=False)
    (archive / "openapi.json").write_bytes(API_SCHEMA.read_bytes())
    write_json(archive / "metadata.json", metadata)
    return {"path": str(archive.resolve()), "metadata": metadata}


def validate_registered_retrieval(inputs: dict[str, Any]) -> dict[str, Any]:
    receipt = inputs["retrieval"]
    archive = Path(receipt["path"])
    metadata = json.loads((archive / "metadata.json").read_text())
    if (
        metadata != receipt["metadata"]
        or sha256(archive / "openapi.json") != metadata["api_schema_sha256"]
    ):
        raise ValueError("Registered LeanSearch metadata or API schema changed")
    manifest = load_manifest(Path(inputs["bundle"]))
    if any(metadata[key] != manifest[key] for key in ("lean_toolchain", "mathlib_revision")):
        raise ValueError("Registered LeanSearch and experiment versions differ")
    settings = registered_settings()
    if (
        metadata["tool_config"] != settings["tool_config"]
        or metadata["mathlib_release"] != settings["mathlib_release"]
    ):
        raise ValueError("Registered LeanSearch settings changed")
    return metadata


SERVER_URL = "https://leansearch.net"
SEARCH_URL = SERVER_URL + "/search"


class NativeSearchIntegrityError(BaseException):
    """Bypass upstream's conversion of ordinary search exceptions into model feedback."""


class _SearchResponse:
    def __init__(self, status: int, body: str, url: str):
        self.status, self.body, self.url = status, body, url
        self.content_length = len(body.encode())

    def raise_for_status(self):
        if self.status >= 400:
            from types import SimpleNamespace

            import aiohttp

            raise aiohttp.ClientResponseError(
                SimpleNamespace(real_url=self.url),
                (),
                status=self.status,
                message="LeanSearch API request failed",
                headers={},
            )

    async def json(self):
        return json.loads(self.body)

    async def text(self):
        return self.body


class _SearchSession:
    def __init__(self, session, retrieval, archive_dir, request_scope, archive_records):
        from .protocol import content_hash

        if retrieval["tool_config"]["server_url"] != SERVER_URL:
            raise NativeSearchIntegrityError("Only the approved LeanSearch API may be used")
        self.session = session
        self.retrieval = retrieval
        self.archive_dir = Path(archive_dir)
        self.request_scope = request_scope
        self.archive_records = archive_records if archive_records is not None else {}
        self.fingerprint = content_hash(retrieval)

    def post(self, url, **kwargs):
        import asyncio
        from contextlib import asynccontextmanager
        from datetime import datetime
        from uuid import uuid4

        import aiohttp

        from .controller import atomic_json
        from .protocol import content_hash

        @asynccontextmanager
        async def request():
            if str(url) != SEARCH_URL:
                raise NativeSearchIntegrityError("Request outside the approved LeanSearch API")
            if set(kwargs) - {"json", "headers", "timeout"}:
                raise NativeSearchIntegrityError("Unsupported LeanSearch request options")
            payload = kwargs.get("json")
            if not (
                isinstance(payload, dict)
                and set(payload) == {"query", "num_results"}
                and isinstance(payload["query"], list)
                and len(payload["query"]) == 1
                and isinstance(payload["query"][0], str)
                and type(payload["num_results"]) is int
                and payload["num_results"] == self.retrieval["tool_config"]["max_results"]
            ):
                raise NativeSearchIntegrityError(
                    "Request differs from the registered search protocol"
                )
            headers = kwargs.get("headers", {})
            if any(key.lower() not in {"accept", "content-type", "user-agent"} for key in headers):
                raise NativeSearchIntegrityError("Unregistered LeanSearch request headers")
            scope = self.request_scope() if self.request_scope else None
            identity = {
                "scope": scope,
                "url": SEARCH_URL,
                "request": payload,
                "retrieval_sha256": self.fingerprint,
            }
            name = content_hash(scope) if scope is not None else uuid4().hex
            path = self.archive_dir / f"{name}.json"
            if name in self.archive_records and not path.is_file():
                raise NativeSearchIntegrityError("Saved LeanSearch replay record is missing")
            if path.exists():
                try:
                    record = json.loads(path.read_text())
                    digest = record.pop("record_sha256")
                    valid = (
                        digest == content_hash(record)
                        and (
                            name not in self.archive_records or self.archive_records[name] == digest
                        )
                        and all(record.get(k) == v for k, v in identity.items())
                        and (
                            isinstance(record.get("integrity_error"), str)
                            or (
                                isinstance(record.get("transport_error"), dict)
                                and isinstance(record["transport_error"].get("message"), str)
                                and type(record["transport_error"].get("timeout")) is bool
                            )
                            or (
                                type(record.get("status")) is int
                                and 100 <= record["status"] <= 599
                                and isinstance(record.get("response_body"), str)
                            )
                        )
                    )
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                    raise NativeSearchIntegrityError(
                        "Saved LeanSearch replay record is invalid"
                    ) from error
                if not valid:
                    raise NativeSearchIntegrityError(
                        "LeanSearch replay differs from its saved request"
                    )
            else:
                record = {**identity, "recorded_at": datetime.now(UTC).isoformat()}
                try:
                    async with self.session.post(url, **kwargs, allow_redirects=False) as response:
                        record["status"] = response.status
                        record["response_body"] = await response.text()
                        if 300 <= response.status < 400:
                            record["integrity_error"] = "LeanSearch redirects are forbidden"
                except NativeSearchIntegrityError as error:
                    record["integrity_error"] = str(error)
                except (TimeoutError, aiohttp.ClientError) as error:
                    record["transport_error"] = {
                        "timeout": isinstance(error, asyncio.TimeoutError),
                        "message": str(error),
                    }
                digest = content_hash(record)
                try:
                    self.archive_dir.mkdir(parents=True, exist_ok=True)
                    atomic_json(path, {**record, "record_sha256": digest})
                except OSError as error:
                    raise NativeSearchIntegrityError(
                        "Could not preserve the LeanSearch response"
                    ) from error
            if scope is not None:
                self.archive_records[name] = digest
            if record.get("integrity_error"):
                raise NativeSearchIntegrityError(record["integrity_error"])
            if error := record.get("transport_error"):
                if error["timeout"]:
                    raise TimeoutError(error["message"])
                raise aiohttp.ClientError(error["message"])
            yield _SearchResponse(record["status"], record["response_body"], SEARCH_URL)

        return request()


def checked_session(session, retrieval, archive_dir, *, request_scope=None, archive_records=None):
    """Restrict native HTTP requests and replay archived responses on continuation."""
    return _SearchSession(session, retrieval, archive_dir, request_scope, archive_records)


async def validate_retrieval_live(retrieval):
    if retrieval["tool_config"]["server_url"] != SERVER_URL:
        raise ValueError("Only the approved LeanSearch API may be used")
