#!/usr/bin/env python3
"""The web seam's three surfaces (§8.7), against the stub device.

The point of these tests is the *contract*: state is sent whole, a verb is one
`POST` returning the resulting state or a typed error, and the SSE stream is
the only push channel. The browser never reaches the port, so nothing here
needs a board.

Requires `aiohttp`; skipped without it (the pure suites do not).

Run:  python3 -m unittest discover -s container/tests -p 'test_ns2web*.py'
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import support  # noqa: E402

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAVE_AIOHTTP = True
except ImportError:  # pragma: no cover - exercised only without aiohttp
    HAVE_AIOHTTP = False

from container.ns2container import Settings, create_app  # noqa: E402
from container.ns2device import StubDevice  # noqa: E402
from container.ns2sealing import KeyMaterial, SealedTag, identity_of  # noqa: E402

#: The sealing fixture: a synthetic key and a tag that verifies under it.
TAG = support.sealing_fixture("tag")
KEY_BYTES = support.sealing_fixture("key")


def fake_sealer(image: bytes, key: KeyMaterial, *, identity: bytes | None = None) -> SealedTag:
    return SealedTag(image=image, identity=identity or identity_of(image))


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp is not installed")
class WebSeamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "macros").mkdir()
        (root / "amiibo" / "Zelda").mkdir(parents=True)
        (root / "keys").mkdir()
        shutil.copy(support.correction_macro_path(), root / "macros" / "correction.json")
        (root / "amiibo" / "Zelda" / "Link.bin").write_bytes(TAG)
        (root / "keys" / "key_retail.bin").write_bytes(KEY_BYTES)
        static = root / "dist"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<!doctype html><title>ns2</title>")
        (static / "assets" / "app.js").write_text("console.log('ns2')")

        settings = Settings(
            macro_dir=root / "macros",
            amiibo_dir=root / "amiibo",
            key_file=root / "keys" / "key_retail.bin",
            key_dir=root / "keys",
            static_dir=static,
        )
        app = create_app(settings, device=StubDevice(), sealer=fake_sealer)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_state_is_sent_whole(self):
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIn("state", payload)
        self.assertIn("logs", payload)
        self.assertEqual(payload["state"]["control"]["link"], "UP")
        self.assertEqual(payload["state"]["key"], "KEY_OK")

    async def test_a_verb_returns_the_resulting_state(self):
        response = await self.client.post("/api/start", json={"macroId": "correction.json"})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["state"]["mode"], "MACRO")
        stop = await self.client.post("/api/stop")
        self.assertEqual((await stop.json())["state"]["mode"], "IDLE")

    async def test_a_refused_verb_is_a_typed_error(self):
        response = await self.client.post("/api/start", json={"macroId": "missing.json"})
        self.assertEqual(response.status, 409)
        payload = await response.json()
        self.assertEqual(payload["error"]["code"], "UNKNOWN_MACRO")

    async def test_a_malformed_body_is_a_bad_request(self):
        response = await self.client.post("/api/start", data="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)

    async def test_place_and_unplace(self):
        placed = await self.client.post("/api/place", json={"figureId": "Zelda/Link.bin"})
        self.assertEqual(placed.status, 200)
        payload = await placed.json()
        self.assertEqual(payload["state"]["mode"], "AMIIBO")
        unplaced = await self.client.post("/api/unplace")
        self.assertEqual((await unplaced.json())["state"]["mode"], "IDLE")

    async def test_rescan_is_container_local(self):
        response = await self.client.post("/api/rescan")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["state"]["macroLibrary"], "READY")

    async def test_the_static_ui_is_served(self):
        index = await self.client.get("/")
        self.assertEqual(index.status, 200)
        self.assertIn("ns2", await index.text())
        asset = await self.client.get("/assets/app.js")
        self.assertEqual(asset.status, 200)

    async def test_the_sse_stream_opens_with_a_state_event(self):
        response = await self.client.get("/api/events")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        chunk = await asyncio.wait_for(response.content.read(32), timeout=2)
        self.assertTrue(chunk.startswith(b"event: state"))
        response.close()

    async def test_healthz(self):
        response = await self.client.get("/healthz")
        self.assertEqual((await response.json()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
