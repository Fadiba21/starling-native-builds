from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import upload_result
from validate_inputs import validate_ask, validate_gateway, validate_tools

ASK = "a" * 32


class MockResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


class MockHTTP:
    """In-memory mock transport; no gateway or expensive native build is used."""

    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return MockResponse()


class GatewayPublisherTests(unittest.TestCase):
    def write_result(self, directory: pathlib.Path, data: bytes, status: dict) -> None:
        (directory / "libhorizon_android.so").write_bytes(data)
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def test_success_uses_building_then_zero_indexed_1mib_puts_then_done(self):
        data = b"fixture-library" + bytes(upload_result.CHUNK_SIZE) + b"tail"
        metadata = {"game": "0.8.0", "target": "aarch64-linux-android"}
        status = {"state": "done", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "build": metadata}
        with tempfile.TemporaryDirectory() as temp:
            self.write_result(pathlib.Path(temp), data, status)
            http = MockHTTP()
            self.assertTrue(upload_result.publish("https://gateway.example", ASK, "mock-key", temp, "success", http))

        self.assertEqual([req.get_method() for req in http.requests], ["POST", "PUT", "PUT", "POST"])
        self.assertTrue(http.requests[0].full_url.endswith(f"/v1/internal/job/{ASK}/status"))
        self.assertTrue(http.requests[1].full_url.endswith(f"/v1/internal/job/{ASK}/part/0"))
        self.assertTrue(http.requests[2].full_url.endswith(f"/v1/internal/job/{ASK}/part/1"))
        self.assertEqual([len(req.data) for req in http.requests[1:3]], [upload_result.CHUNK_SIZE, len(data) - upload_result.CHUNK_SIZE])
        for req in http.requests:
            self.assertEqual(req.get_header("Authorization"), "Bearer mock-key")
        self.assertEqual(json.loads(http.requests[0].data), {"state": "building"})
        final = json.loads(http.requests[-1].data)
        self.assertEqual(final, {
            "state": "done", "sha256": status["sha256"], "size": len(data),
            "build": metadata, "parts": 2, "chunk_size": upload_result.CHUNK_SIZE,
        })

    def test_failed_build_sends_official_failure_and_no_parts(self):
        status = {"state": "failed", "error": "Native mod does not compile.", "settled": True}
        with tempfile.TemporaryDirectory() as temp:
            (pathlib.Path(temp) / "status.json").write_text(json.dumps(status), encoding="utf-8")
            http = MockHTTP()
            self.assertFalse(upload_result.publish("https://gateway.example", ASK, "mock-key", temp, "success", http))
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0].get_method(), "POST")
        self.assertEqual(json.loads(http.requests[0].data), status)

    def test_prebuild_failure_uses_generic_error(self):
        with tempfile.TemporaryDirectory() as temp:
            http = MockHTTP()
            self.assertFalse(upload_result.publish("https://gateway.example", ASK, "mock-key", temp, "skipped", http))
        self.assertEqual(json.loads(http.requests[0].data), {
            "state": "failed", "error": "Build did not complete successfully.", "settled": False,
        })

    def test_hash_mismatch_is_rejected_before_parts_and_failure_callback_attempted(self):
        data = b"library"
        status = {"state": "done", "sha256": "0" * 64, "size": len(data), "build": {}}
        with tempfile.TemporaryDirectory() as temp:
            self.write_result(pathlib.Path(temp), data, status)
            http = MockHTTP()
            with self.assertRaises(upload_result.PublishError):
                upload_result.publish("https://gateway.example", ASK, "mock-key", temp, "success", http)
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(json.loads(http.requests[0].data)["state"], "failed")
        self.assertNotIn("secret", json.dumps(json.loads(http.requests[0].data)))

    def test_dispatch_input_constraints(self):
        self.assertEqual(validate_ask(ASK), ASK)
        for bad in ("A" * 32, "../etc/passwd", "a" * 31, "x" * 33, "0" * 31 + "g"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_ask(bad)
        self.assertEqual(validate_gateway("https://Worker.Example"), "https://worker.example")
        for bad in (
            "http://worker.example", "https://user:pass@worker.example",
            "https://worker.example/path", "https://127.0.0.1", "https://worker.example?q=1",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_gateway(bad)

    def test_tools_input_uses_valid_nonblank_default(self):
        self.assertEqual(validate_tools("starling"), "starling")
        with self.assertRaises(ValueError):
            validate_tools("")


if __name__ == "__main__":
    unittest.main()
