import os
import sys
import time
import fcntl
import socket
import subprocess
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, "ysp-live-v9.5.py")
LOCAL_PORT = 8768
LOCK_FILE = "/tmp/ysp-backend.lock"


def port_ready():
    try:
        with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=1):
            return True
    except OSError:
        return False


def start_backend():
    os.makedirs("/tmp/ysp-data", exist_ok=True)

    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if port_ready():
            return

        env = os.environ.copy()
        env["YSP_DATA_DIR"] = "/tmp/ysp-data"

        subprocess.Popen(
            [
                sys.executable,
                SCRIPT,
                str(LOCAL_PORT),
                "--bind", "127.0.0.1",
                "--no-engine"
            ],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=open("/tmp/ysp-backend.log", "a"),
            stderr=subprocess.STDOUT
        )

        for _ in range(30):
            if port_ready():
                return
            time.sleep(1)

                try:
            with open("/tmp/ysp-backend.log", "r", encoding="utf-8", errors="replace") as f:
                log = f.read()[-6000:]
        except Exception as e:
            log = "日志读取失败：" + str(e)

        raise RuntimeError(
            "IPTV 后端启动超时\n后端日志：\n" + log
        )


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

        request = urllib.request.Request(
            url, headers=headers, method=method
        )

        try:
            response = urllib.request.urlopen(request, timeout=60)
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
                    data = response.read(65536)
                    if not data:
                        break
                    yield data
            finally:
                response.close()

        return stream()

    except Exception as e:
        body = str(e).encode("utf-8")
        start_response(
            "502 Bad Gateway",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body)))
            ]
        )
        return [body]
app = application
