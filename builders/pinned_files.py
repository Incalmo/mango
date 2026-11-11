"""Serve only explicitly mapped local inputs while an authored command runs."""

import argparse
import http.server
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path


def normalized_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(part in {"", ".", ".."} for part in path[1:].split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(char in path for char in "\\?#")
    ):
        raise ValueError("expected a normalized absolute URL path")
    return path


def request_path(target):
    if "?" in target or "#" in target or re.search(r"%(?![0-9a-fA-F]{2})", target):
        raise ValueError("queries, fragments and malformed escapes are unsupported")
    # Exactly one decode. An authored literal %2f requires %252f on the wire.
    return normalized_path(
        urllib.parse.unquote(target, encoding="utf-8", errors="strict")
    )


def load_map(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate map key: {key}")
            result[key] = value
        return result

    document = json.loads(Path(path).read_text(), object_pairs_hook=unique_pairs)
    if not isinstance(document, dict):
        raise ValueError("input map must be an object")
    mapped = {}
    for route, entry in document.items():
        normalized_path(route)
        if not isinstance(entry, dict) or set(entry) - {"file", "content_type"}:
            raise ValueError(f"invalid map entry: {route}")
        source = Path(entry["file"])
        if not source.is_absolute() or not source.is_file():
            raise ValueError(
                f"mapped file must be an existing absolute regular file: {route}"
            )
        content_type = entry.get("content_type", "application/octet-stream")
        if not isinstance(content_type, str) or any(
            ord(c) < 32 or ord(c) == 127 for c in content_type
        ):
            raise ValueError(f"invalid content_type: {route}")
        mapped[route] = (source.resolve(strict=True), content_type)
    return mapped


class PinnedServer(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def __init__(self, mapped, port):
        self.mapped = mapped
        self.failures = []
        self.failure_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), PinnedHandler)

    def failure(self, method, target, reason):
        message = f"pinned files: {reason}: {method} {target}"
        with self.failure_lock:
            self.failures.append(message)
            print(json.dumps({"error": message}), file=sys.stderr, flush=True)


class PinnedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def parse_request(self):
        accepted = super().parse_request()
        if accepted:
            # Preserve the wire target: http.server otherwise normalizes // paths.
            self.path = self.requestline.split()[1]
        return accepted

    def send_error(self, code, message=None, explain=None):
        self.server.failure(
            getattr(self, "command", "?"), getattr(self, "path", "?"), f"HTTP {code}"
        )
        super().send_error(code, message, explain)

    def do_GET(self):
        self.serve_file(False)

    def do_HEAD(self):
        self.serve_file(True)

    def serve_file(self, head):
        try:
            route = request_path(self.path)
        except ValueError:
            self.send_error(400, "invalid pinned-file URL")
            return
        entry = self.server.mapped.get(route)
        if entry is None:
            self.server.failure(self.command, self.path, "not in pinned file map")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        source, content_type = entry
        try:
            with source.open("rb") as body:
                info = os.fstat(body.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("mapped input is no longer a regular file")
                self.send_response(200)
                self.send_header("Content-Length", str(info.st_size))
                self.send_header("Content-Type", content_type)
                self.end_headers()
                if not head:
                    while chunk := body.read(1024 * 1024):
                        self.wfile.write(chunk)
        except (OSError, ValueError) as error:
            self.server.failure(
                self.command,
                self.path,
                f"cannot serve mapped input ({type(error).__name__})",
            )
            self.close_connection = True


def run(mapped, port, command):
    with PinnedServer(mapped, port) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        child = None
        interrupted = []
        previous = {}

        def interrupt(signum, _frame):
            interrupted.append(signum)
            if child is not None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, interrupt)
            environment = os.environ.copy()
            environment["INCALMO_PINNED_FILES_URL"] = (
                f"http://127.0.0.1:{server.server_port}"
            )
            child = subprocess.Popen(command, env=environment, start_new_session=True)
            deadline = None
            while child.poll() is None:
                if interrupted:
                    if deadline is None:
                        deadline = time.monotonic() + 2
                    if time.monotonic() >= deadline:
                        os.killpg(child.pid, signal.SIGKILL)
                try:
                    child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            code = child.returncode
        finally:
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    time.sleep(0.05)
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            server.shutdown()
            thread.join()
            server.server_close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        if interrupted:
            return 128 + interrupted[0]
        if code:
            return code if code > 0 else 128 - code
        return 1 if server.failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 0 <= args.port <= 65535:
        parser.error("supply a command after -- and a port from 0 to 65535")
    try:
        return run(load_map(args.map), args.port, command)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"pinned files: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
