
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
PID_FILE = "/tmp/ysp-backend.pid"
LOG_FILE = "/tmp/ysp-backend.log"
DATA_DIR = "/tmp/ysp-data"


def port_ready():
    try:
        with socket.create_connection(
            ("127.0.0.1", LOCAL_PORT), timeout=2
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


def start_backend():
    os.makedirs(DATA_DIR, exist_ok=True)

    with open(LOCK_FILE, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        # 已经启动并监听端口，直接使用
        if port_ready():
            return

        # 已有后端进程，等待它就绪，不重复启动
        if backend_process_running():
            for _ in range(60):
                if port_ready():
                    return
                time.sleep(1)

            raise RuntimeError(
                "IPTV 后端进程存在，但 8768 端口未就绪"
            )

        # 清理旧 PID 文件
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
                    "--bind", "127.0.0.1",
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

        # 等待后端启动
        for _ in range(60):
            if port_ready():
                return

            if process.poll() is not None:
                break

            time.sleep(1)

        try:
            with open(LOG_FILE, "r", encoding="utf-8",
                      errors="replace") as f:
                log_text = f.read()[-6000:]
        except Exception as e:
            log_text = "日志读取失败：" + str(e)

        raise RuntimeError(
            "IPTV 后端启动失败或超时\n后端日志：\n" + log_text
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

        for key in (
            "HTTP_USER_AGENT",
            "HTTP_ACCEPT",
            "HTTP_RANGE"
        ):
            if environ.get(key):
                headers[
                    key[5:].replace("_", "-").title()
                ] = environ[key]

        request = urllib.request.Request(
            url,
            headers=headers,
            method=method
        )

        try:
            response = urllib.request.urlopen(
                request, timeout=60
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
