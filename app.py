
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

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8768

LOCK_FILE = "/tmp/ysp-backend.lock"
PID_FILE = "/tmp/ysp-backend.pid"
LOG_FILE = "/tmp/ysp-backend.log"
DATA_DIR = "/tmp/ysp-data"

# 首次启动最多等待25秒
START_WAIT_SECONDS = 25


def port_ready():
    try:
        with socket.create_connection(
            (LOCAL_HOST, LOCAL_PORT), timeout=2
        ):
            return True
    except OSError:
        return False


def backend_process_running():
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode(errors="replace")

        return SCRIPT in cmdline
    except (OSError, ValueError):
        return False


def read_backend_log():
    try:
        with open(LOG_FILE, "r", encoding="utf-8",
                  errors="replace") as f:
            return f.read()[-5000:]
    except OSError as e:
        return "日志读取失败：" + str(e)


def start_backend():
    if not os.path.isfile(SCRIPT):
        raise RuntimeError("找不到后端文件：" + SCRIPT)

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(LOCK_FILE, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if port_ready():
            return

        # 已有进程但端口未就绪，不重复启动
        if backend_process_running():
            raise RuntimeError(
                "IPTV 后端正在启动，端口尚未就绪，请稍后重试。"
            )

        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass

        env = os.environ.copy()
        env["YSP_DATA_DIR"] = DATA_DIR

        with open(LOG_FILE, "a") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    SCRIPT,
                    str(LOCAL_PORT),
                    "--bind", LOCAL_HOST,
                    "--no-engine"
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )

        with open(PID_FILE, "w") as f:
            f.write(str(process.pid))

        # 等待首次启动，避免请求无限等待
        deadline = time.monotonic() + START_WAIT_SECONDS

        while time.monotonic() < deadline:
            if port_ready():
                return

            if process.poll() is not None:
                break

            time.sleep(1)

        raise RuntimeError(
            "IPTV 后端未能在25秒内就绪，请检查后端日志。"
            "\n" + read_backend_log()
        )


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()

    try:
        start_backend()

        path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")

        url = f"http://{LOCAL_HOST}:{LOCAL_PORT}{path}"

        if query:
            url += "?" + query

        headers = {}

        for key in (
            "HTTP_USER_AGENT",
            "HTTP_ACCEPT",
            "HTTP_RANGE",
            "HTTP_AUTHORIZATION",
            "HTTP_COOKIE",
            "CONTENT_TYPE"
        ):
            if environ.get(key):
                headers[
                    key[5:].replace("_", "-").title()
                ] = environ[key]

        body = None

        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError:
                length = 0

            if length > 0:
                body = environ["wsgi.input"].read(length)

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method
        )

        try:
            response = urllib.request.urlopen(
                request, timeout=45
            )
        except urllib.error.HTTPError as e:
            response = e

        status = f"{response.status} {response.reason}"

        response_headers = [
            (k, v)
            for k, v in response.headers.items()
            if k.lower() not in (
                "connection",
                "transfer-encoding",
                "keep-alive"
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
        message = str(e).encode(
            "utf-8", errors="replace"
        )

        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(message))),
                ("Retry-After", "5")
            ]
        )

        return [message]


app = application
