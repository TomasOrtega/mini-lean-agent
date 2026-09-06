import json
from contextlib import asynccontextmanager

import pytest

from paper_experiments import retrieval


@asynccontextmanager
async def local_search(monkeypatch, *, redirect=False):
    from aiohttp import web

    calls = []

    async def search(request):
        calls.append(await request.json())
        if redirect:
            return web.Response(status=307, headers={"Location": "/forbidden"})
        return web.json_response(
            [
                [
                    {
                        "result": {
                            "name": ["Nat", "add_comm"],
                            "kind": "theorem",
                            "signature": "a + b = b + a",
                        }
                    }
                ]
            ]
        )

    async def forbidden(request):
        pytest.fail("The client followed a redirect outside the approved API")

    app = web.Application()
    app.router.add_post("/search", search)
    app.router.add_post("/forbidden", forbidden)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(retrieval, "SERVER_URL", base)
    monkeypatch.setattr(retrieval, "SEARCH_URL", base + "/search")
    try:
        yield base, calls
    finally:
        await runner.cleanup()


def config(url):
    return {
        "tool_config": {
            "tool_type": "search_lean_search",
            "server_url": url,
            "max_results": 6,
            "timeout": 60,
            "max_retries": 3,
            "retry_delay": 2,
        }
    }


@pytest.mark.asyncio
async def test_only_search_post_allowed_and_raw_response_archived(tmp_path, monkeypatch):
    import aiohttp

    async with local_search(monkeypatch) as (base, calls):
        async with aiohttp.ClientSession() as session:
            checked = retrieval.checked_session(session, config(base), tmp_path)
            async with checked.post(
                base + "/search", json={"query": ["addition commutes"], "num_results": 6}
            ) as response:
                response.raise_for_status()
                data = await response.json()
            assert data[0][0]["result"]["name"] == ["Nat", "add_comm"]
            for url in [
                base + "/fetch",
                base + "/search?next=elsewhere",
                "https://example.org/search",
            ]:
                with pytest.raises(retrieval.NativeSearchIntegrityError):
                    async with checked.post(url, json={"query": ["test"], "num_results": 6}):
                        pass
        assert calls == [{"query": ["addition commutes"], "num_results": 6}]
        archive = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert archive["request"] == calls[0]
        assert json.loads(archive["response_body"]) == data


@pytest.mark.asyncio
async def test_search_redirect_is_never_followed(tmp_path, monkeypatch):
    import aiohttp

    async with local_search(monkeypatch, redirect=True) as (base, calls):
        async with aiohttp.ClientSession() as session:
            checked = retrieval.checked_session(session, config(base), tmp_path)
            with pytest.raises(retrieval.NativeSearchIntegrityError, match="redirect"):
                async with checked.post(
                    base + "/search", json={"query": ["test"], "num_results": 6}
                ):
                    pass
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_continuation_replays_search_without_requerying_service(tmp_path, monkeypatch):
    import aiohttp

    scope = {"node": "proposer", "step": 2, "search_call": 1}
    async with local_search(monkeypatch) as (base, calls), aiohttp.ClientSession() as session:
        checked = retrieval.checked_session(
            session, config(base), tmp_path, request_scope=lambda: scope
        )
        results = []
        for _ in range(2):
            async with checked.post(
                base + "/search", json={"query": ["same query"], "num_results": 6}
            ) as response:
                results.append(await response.json())
        assert results[0] == results[1]
        assert len(calls) == 1
        with pytest.raises(retrieval.NativeSearchIntegrityError, match="replay"):
            async with checked.post(
                base + "/search", json={"query": ["changed query"], "num_results": 6}
            ):
                pass


@pytest.mark.asyncio
async def test_transport_refuses_url_encoded_credentials(tmp_path):
    class NoNetwork:
        def post(self, *args, **kwargs):
            pytest.fail("Unapproved request was dispatched")

    checked = retrieval.checked_session(NoNetwork(), config("https://leansearch.net"), tmp_path)
    with pytest.raises(retrieval.NativeSearchIntegrityError):
        async with checked.post(
            "https://user:secret@leansearch.net/search", json={"query": ["test"], "num_results": 6}
        ):
            pass


@pytest.mark.asyncio
async def test_corrupt_search_archive_is_fatal_not_model_feedback(tmp_path, monkeypatch):
    import aiohttp

    scope = {"node": "proposer", "step": 2, "search_call": 1}
    async with local_search(monkeypatch) as (base, calls), aiohttp.ClientSession() as session:
        checked = retrieval.checked_session(
            session, config(base), tmp_path, request_scope=lambda: scope
        )
        async with checked.post(base + "/search", json={"query": ["same query"], "num_results": 6}):
            pass
        next(tmp_path.glob("*.json")).write_text("{truncated")
        with pytest.raises(retrieval.NativeSearchIntegrityError):
            async with checked.post(
                base + "/search", json={"query": ["same query"], "num_results": 6}
            ):
                pass
        assert len(calls) == 1
