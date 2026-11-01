import os
import sys
import time
import atexit
import socket
import threading
import subprocess
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, "ysp-live-v9.5.py")
LOCAL_PORT = 8768

_lock = threading.Lock()
_process = None


def start_backend():
    global _process

    with _lock:
        if _process and _process.poll() is None:
            return

        env = os.environ.copy()
        env["YSP_DATA_DIR"] = "/tmp/ysp-data"
        os.makedirs(env["YSP_DATA_DIR"], exist_ok=True)

        _process = subprocess.Popen([
            sys.executable,
            SCRIPT,
            str(LOCAL_PORT),
            "--bind", "127.0.0.1",
            "--no-engine"
        ], cwd=ROOT, env=env)

        for _ in range(30):
            try:
                with socket.create_connection(
                    ("127.0.0.1", LOCAL_PORT), timeout=1
                ):
                    return
            except OSError:
                time.sleep(1)

        raise RuntimeError("IPTV 后端启动失败")


def application(environ, start_response):
    try:
        start_backend()

        path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")
        url = f"http://127.0.0.1:{LOCAL_PORT}{path}"
        if query:
            url += "?" + query

        method = environ.get("REQUEST_METHOD", "GET")
        headers = {}

        for key in ("HTTP_USER_AGENT", "HTTP_ACCEPT", "HTTP_RANGE"):
            if environ.get(key):
                headers[key[5:].replace("_", "-").title()] = environ[key]

        req = urllib.request.Request(url, headers=headers, method=method)

        try:
            response = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            response = e

        status = f"{response.status} {response.reason}"
        response_headers = [
            (k, v) for k, v in response.headers.items()
            if k.lower() not in (
                "connection", "transfer-encoding", "keep-alive"
            )
        ]

        start_response(status, response_headers)

        if method == "HEAD":
            response.close()
            return [b""]

        def stream():
            try:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                response.close()

        return stream()

    except Exception as e:
        body = str(e).encode("utf-8")
        start_response(
            "502 Bad Gateway",
            [("Content-Type", "text/plain; charset=utf-8"),
             ("Content-Length", str(len(body)))]
        )
        return [body]


def cleanup():
    if _process and _process.poll() is None:
        _process.terminate()


atexit.register(cleanup)
