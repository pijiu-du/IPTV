import os
import sys
import time
import fcntl
import socket
import subprocess
import urllib.request
import urllib.error
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, "ysp-live-v9.5.py")

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8768

LOCK_FILE = "/tmp/ysp-backend.lock"
PID_FILE = "/tmp/ysp-backend.pid"
LOG_FILE = "/tmp/ysp-backend.log"
DATA_DIR = os.environ.get("YSP_DATA_DIR", "/tmp/ysp-data")

START_WAIT_SECONDS = 25

# 诊断密钥：请保留此值
DIAG_TOKEN = "KM-YSP-2026-11-02-a83f92c71e6b"


# =========================================================
# 端口连接测试
# =========================================================

def port_test():
    try:
        with socket.create_connection(
            (LOCAL_HOST, LOCAL_PORT), timeout=2
        ):
            return True, "连接成功"

    except OSError as e:
        return False, (
            "连接失败：类型={}，errno={}，错误={}".format(
                type(e).__name__,
                getattr(e, "errno", None),
                str(e)
            )
        )


def port_ready():
    return port_test()[0]


# =========================================================
# 后端进程信息
# =========================================================

def get_backend_pid():
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        if pid <= 1:
            return None

        with open("/proc/{}/cmdline".format(pid), "rb") as f:
            cmdline = f.read().decode(errors="replace")

        if SCRIPT in cmdline:
            return pid

    except (OSError, ValueError):
        pass

    return None


def get_process_info():
    pid = get_backend_pid()

    if not pid:
        return "后端进程：不存在或 PID 文件无效"

    try:
        with open("/proc/{}/status".format(pid), "r") as f:
            status = f.read()

        state = "未知"

        for line in status.splitlines():
            if line.startswith("State:"):
                state = line.strip()
                break

        with open("/proc/{}/cmdline".format(pid), "rb") as f:
            cmdline = f.read().decode(errors="replace")
            cmdline = cmdline.replace("\x00", " ")

        return (
            "后端 PID：{}\n".format(pid)
            + "进程状态：{}\n".format(state)
            + "启动命令：{}".format(cmdline)
        )

    except OSError as e:
        return "读取进程信息失败：" + str(e)


# =========================================================
# 网络命名空间诊断
# =========================================================

def read_netns(path):
    try:
        return os.readlink(path)
    except OSError as e:
        return "读取失败：" + str(e)


# =========================================================
# TCP 监听表诊断
# =========================================================

def decode_tcp_address(hex_address, ipv6=False):
    try:
        raw = bytes.fromhex(hex_address)

        if ipv6:
            raw = b"".join(
                raw[i:i + 4][::-1]
                for i in range(0, 16, 4)
            )
            return socket.inet_ntop(socket.AF_INET6, raw)

        raw = raw[::-1]
        return socket.inet_ntop(socket.AF_INET, raw)

    except Exception:
        return hex_address


def read_listen_entries(path):
    results = []

    for filename, ipv6 in (
        ("tcp", False),
        ("tcp6", True)
    ):
        filepath = os.path.join(path, filename)

        try:
            with open(filepath, "r") as f:
                lines = f.readlines()[1:]

            for line in lines:
                fields = line.split()

                if len(fields) < 10:
                    continue

                local = fields[1]
                state = fields[3]
                inode = fields[9]

                try:
                    address_hex, port_hex = local.split(":")
                    port = int(port_hex, 16)
                except (ValueError, IndexError):
                    continue

                # 0A 表示 LISTEN
                if port != LOCAL_PORT or state != "0A":
                    continue

                address = decode_tcp_address(
                    address_hex, ipv6
                )

                results.append({
                    "file": filename,
                    "address": address,
                    "port": port,
                    "inode": inode
                })

        except OSError as e:
            results.append({
                "file": filename,
                "error": str(e)
            })

    return results


# =========================================================
# 检查监听 socket 是否属于后端进程
# =========================================================

def check_socket_owner(pid, entries):
    if not pid:
        return "没有有效后端 PID，无法检查"

    target_inodes = {
        item["inode"]
        for item in entries
        if "inode" in item
    }

    if not target_inodes:
        return "没有发现 8768 LISTEN socket"

    found = []
    fd_path = "/proc/{}/fd".format(pid)

    try:
        for fd in os.listdir(fd_path):
            filepath = os.path.join(fd_path, fd)

            try:
                target = os.readlink(filepath)
            except OSError:
                continue

            for inode in target_inodes:
                if target == "socket:[{}]".format(inode):
                    found.append(
                        "FD {} -> {}".format(fd, target)
                    )

    except OSError as e:
        return "读取后端 FD 失败：" + str(e)

    if found:
        return "是，找到监听 socket：\n" + "\n".join(found)

    return "否，后端 PID 的 FD 中没有找到对应监听 socket"


# =========================================================
# 后端日志
# =========================================================

def read_backend_log():
    try:
        with open(
            LOG_FILE,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 10000))
            return f.read()

    except OSError as e:
        return "日志读取失败：" + str(e)


# =========================================================
# 完整诊断
# =========================================================

def diagnostic():
    pid = get_backend_pid()

    # Gunicorn 当前 worker 的网络命名空间
    worker_netns = read_netns("/proc/self/ns/net")

    # IPTV 后端进程的网络命名空间
    backend_netns = (
        read_netns("/proc/{}/ns/net".format(pid))
        if pid else "没有有效后端 PID"
    )

    # 分别读取两个进程所在网络命名空间的 TCP 表
    worker_entries = read_listen_entries("/proc/self/net")

    backend_entries = (
        read_listen_entries("/proc/{}/net".format(pid))
        if pid else []
    )

    connected, connect_detail = port_test()

    lines = [
        "IPTV 后端诊断",
        "==============================",
        "Python 版本：{}".format(sys.version),
        "后端文件存在：{}".format(os.path.isfile(SCRIPT)),
        "数据目录存在：{}".format(os.path.isdir(DATA_DIR)),

        "",
        "========== 网络命名空间 ==========",
        "Gunicorn worker：{}".format(worker_netns),
        "IPTV 后端进程：{}".format(backend_netns),
        "是否相同：{}".format(
            "是" if worker_netns == backend_netns else "否"
        ),

        "",
        "========== Gunicorn 所见 TCP 监听 =========="
    ]

    if worker_entries:
        for item in worker_entries:
            lines.append(str(item))
    else:
        lines.append(
            "Gunicorn 所在网络命名空间未发现 8768 LISTEN"
        )

    lines.extend([
        "",
        "========== IPTV 后端所在 TCP 监听 =========="
    ])

    if backend_entries:
        for item in backend_entries:
            lines.append(str(item))
    else:
        lines.append(
            "后端所在网络命名空间未发现 8768 LISTEN"
        )

    lines.extend([
        "",
        "========== 监听 socket 所属进程 ==========",
        check_socket_owner(pid, backend_entries),

        "",
        "========== Gunicorn 连接测试 ==========",
        "8768 端口状态：{}".format(
            "正常连接" if connected else "无法连接"
        ),
        connect_detail,

        "",
        "========== 后端进程 ==========",
        get_process_info(),

        "",
        "========== 后端日志 ==========",
        read_backend_log()
    ])

    return "\n".join(lines)


# =========================================================
# 启动 IPTV 后端
# =========================================================

def start_backend():
    if not os.path.isfile(SCRIPT):
        raise RuntimeError("找不到后端文件：" + SCRIPT)

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(LOCK_FILE, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if port_ready():
            return

        pid = get_backend_pid()

        # 已有真实后端进程：等待，不重复启动
        if pid:
            deadline = time.monotonic() + 8

            while time.monotonic() < deadline:
                if port_ready():
                    return

                time.sleep(1)

            raise RuntimeError(
                "发现 IPTV 后端进程，但 8768 端口无法连接。\n\n"
                + diagnostic()
            )

        # 清理无效 PID 文件
        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass

        env = os.environ.copy()
        env["YSP_DATA_DIR"] = DATA_DIR

        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(
                "\n\n========== 新的后端启动尝试 {} ==========\n".format(
                    time.strftime("%Y-%m-%d %H:%M:%S")
                )
            )
            log.flush()

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

        deadline = time.monotonic() + START_WAIT_SECONDS

        while time.monotonic() < deadline:
            if port_ready():
                return

            if process.poll() is not None:
                break

            time.sleep(1)

        raise RuntimeError(
            "IPTV 后端启动后，8768 端口仍无法连接。\n\n"
            + diagnostic()
        )


# =========================================================
# WSGI 应用入口
# =========================================================

def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")

    # 独立诊断入口，不触发后端启动
    if path == "/__diag":
        query = urllib.parse.parse_qs(
            environ.get("QUERY_STRING", "")
        )

        token = query.get("key", [""])[0]

        if token != DIAG_TOKEN:
            body = b"403 Forbidden"

            start_response(
                "403 Forbidden",
                [
                    ("Content-Type", "text/plain"),
                    ("Content-Length", str(len(body)))
                ]
            )

            return [body]

        body = diagnostic().encode(
            "utf-8",
            errors="replace"
        )

        start_response(
            "200 OK",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body)))
            ]
        )

        return [body]

    try:
        start_backend()

        query = environ.get("QUERY_STRING", "")

        url = "http://{}:{}{}".format(
            LOCAL_HOST,
            LOCAL_PORT,
            path
        )

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
                length = int(
                    environ.get("CONTENT_LENGTH") or "0"
                )
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
                request,
                timeout=45
            )

        except urllib.error.HTTPError as e:
            response = e

        status = "{} {}".format(
            response.status,
            response.reason
        )

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
            "utf-8",
            errors="replace"
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
