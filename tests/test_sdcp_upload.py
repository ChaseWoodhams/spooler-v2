import hashlib
import json
import socket
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from printers.sdcp_upload import UploadError, upload_file


class FakePrinterUpload(BaseHTTPRequestHandler):
    """Accepts /uploadFile/upload chunks the way the Centauri Carbon does."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body)
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            fields[name] = part.get_payload(decode=True)
            if name == "File":
                fields["filename"] = part.get_filename()
        self.server.chunks.append((self.path, fields))
        reply = json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class SdcpUploadTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinterUpload)
        self.server.chunks = []
        self.server.reply = {"code": "000000", "messages": None, "data": {}, "success": True}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cube.gcode"
        self.data = b"".join(f"G1 X{i} Y{i}\n".encode() for i in range(40))
        self.path.write_bytes(self.data)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_file_is_sent_in_ordered_chunks_with_md5_and_upload_id(self):
        progress = []
        upload_file("127.0.0.1", self.path, "cube.gcode", lambda sent, total: progress.append((sent, total)),
                    port=self.port, chunk_size=100)

        chunks = self.server.chunks
        self.assertEqual(len(chunks), -(-len(self.data) // 100))
        self.assertTrue(all(path == "/uploadFile/upload" for path, _ in chunks))
        fields = [f for _, f in chunks]
        self.assertEqual(b"".join(f["File"] for f in fields), self.data)
        self.assertEqual([int(f["Offset"]) for f in fields], list(range(0, len(self.data), 100)))
        self.assertEqual({f["S-File-MD5"] for f in fields}, {hashlib.md5(self.data).hexdigest().encode()})
        self.assertEqual(len({f["Uuid"] for f in fields}), 1)
        self.assertEqual({f["TotalSize"] for f in fields}, {str(len(self.data)).encode()})
        self.assertEqual({f["filename"] for f in fields}, {"cube.gcode"})
        self.assertEqual(progress[-1], (len(self.data), len(self.data)))

    def test_printer_rejection_raises(self):
        self.server.reply = {"code": "111111", "messages": [{"field": "Offset", "message": "-2"}], "success": False}
        with self.assertRaisesRegex(UploadError, "rejected the upload at byte 0"):
            upload_file("127.0.0.1", self.path, "cube.gcode", port=self.port, chunk_size=100)

    def test_unreachable_printer_raises(self):
        with socket.socket() as probe:  # a port with nothing listening
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        with self.assertRaises(UploadError):
            upload_file("127.0.0.1", self.path, "cube.gcode", port=closed_port, timeout=2)


if __name__ == "__main__":
    unittest.main()
