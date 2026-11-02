
import os
import sys
import time
import socket
import fcntl
import subprocess
import urllib.request
import urllib.error
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(BASE_DIR, "ysp-live-v9.5.py")

DATA_DIR = "/tmp/ysp-data"
PID_FILE = "/tmp/ysp-backend.pid"
LOCK_FILE = "/tmp/ysp-backend.lock"
LOG_FILE = "/tmp/ysp-backend.log"

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8768

DIAG_KEY = "KM-YSP-2026-11-02-a83f92c71e6b"


def run_command(args):
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5
        )
        return result.stdout.strip() or "(无输出)"
    except FileNotFoundError:
        return "命令不存在：" + args[0]
    except Exception as e:
        return "执行失败：" + repr(e)


def port_ready():
    try:
        with socket.create_connection(
            (BACKEND_HOST, BACKEND_PORT), timeout=2
        ):
            return True
    except Exception:
        return False


def get_backend_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())

        os.kill(pid, 0)

        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )

        if "ysp-live-v9.5.py" not in cmd:
            return None

        return pid
    except Exception:
        return None


def get_process_info(pid):
    if not pid:
        return "后端进程：不存在或 PID 文件无效"

    result = ["后端 PID：" + str(pid)]

    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("State:"):
                    result.append("进程状态：" + line.strip())
                    break

        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
        result.append("启动命令：" + cmd)

        with open("/proc/%d/ns/net" % pid) as f:
            result.append("后端网络命名空间：" + f.read().strip())

    except Exception as e:
        result.append("读取进程信息失败：" + repr(e))

    return "\n".join(result)


def read_tcp_table():
    lines = []

    for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(filename) as f:
                rows = f.readlines()[1:]

            for row in rows:
                fields = row.split()
                if len(fields) < 10:
                    continue

                local = fields[1]
                state = fields[3]
                queue = fields[4]
                inode = fields[9]

                try:
                    address_hex, port_hex = local.split(":")
                    port = int(port_hex, 16)
                except Exception:
                    continue

                if port != BACKEND_PORT:
                    continue

                state_name = {
                    "0A": "LISTEN",
                    "01": "ESTABLISHED",
                    "02": "SYN_SENT",
                    "03": "SYN_RECV",
                    "04": "FIN_WAIT1",
                    "05": "FIN_WAIT2",
                    "06": "TIME_WAIT",
                    "07": "CLOSE",
                    "08": "CLOSE_WAIT",
                    "09": "LAST_ACK",
                    "0B": "CLOSING"
                }.get(state, "UNKNOWN")

                lines.append(
                    "%s 状态=%s(%s) queue=%s inode=%s 原始记录=%s"
                    % (filename, state, state_name, queue, inode, row.strip())
                )

        except Exception as e:
            lines.append(filename + " 读取失败：" + repr(e))

    return "\n".join(lines) if lines else "没有发现 8768 端口记录"


def check_socket_owner(pid):
    if not pid:
        return "没有有效后端 PID"

    result = []

    try:
        with open("/proc/net/tcp") as f:
            rows = f.readlines()[1:]

        target_inodes = set()

        for row in rows:
            fields = row.split()
            if len(fields) >= 10:
                try:
                    port = int(fields[1].split(":")[1], 16)
                    if port == BACKEND_PORT:
                        target_inodes.add(fields[9])
                except Exception:
                    pass

        fd_dir = "/proc/%d/fd" % pid

        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
                if target.startswith("socket:["):
                    inode = target[8:-1]
                    if inode in target_inodes:
                        result.append("FD %s -> %s" % (fd, target))
            except Exception:
                pass

    except Exception as e:
        return "检查失败：" + repr(e)

    return "\n".join(result) if result else "没有找到对应监听 Socket"


def firewall_diagnostics():
    result = []

    result.append("========== iptables ==========")
    result.append(run_command(["iptables", "-L", "-n", "-v"]))
    result.append("\n========== iptables INPUT ==========")
    result.append(run_command(["iptables", "-L", "INPUT", "-n", "-v"]))
    result.append("\n========== iptables OUTPUT ==========")
    result.append(run_command(["iptables", "-L", "OUTPUT", "-n", "-v"]))
    result.append("\n========== iptables nat ==========")
    result.append(run_command(["iptables", "-t", "nat", "-L", "-n", "-v"]))

    result.append("\n========== ip6tables ==========")
    result.append(run_command(["ip6tables", "-L", "-n", "-v"]))

    result.append("\n========== nftables ==========")
    result.append(run_command(["nft", "list", "ruleset"]))

    result.append("\n========== 回环接口 lo ==========")
    for filename in (
        "/sys/class/net/lo/operstate",
        "/sys/class/net/lo/flags",
        "/sys/class/net/lo/mtu"
    ):
        try:
            with open(filename) as f:
                result.append(filename + " = " + f.read().strip())
        except Exception as e:
            result.append(filename + " 读取失败：" + repr(e))

    result.append("\n========== 网络接口 ==========")
    result.append(run_command(["ip", "addr", "show", "lo"]))

    return "\n".join(result)


def read_backend_log():
    try:
        with open(LOG_FILE, errors="replace") as f:
            content = f.read()
        return content[-12000:] if content else "(日志为空)"
    except Exception as e:
        return "日志不存在或无法读取：" + repr(e)


def diagnostic():
    pid = get_backend_pid()

    result = [
        "IPTV 后端诊断",
        "==============================",
        "Python 版本：" + sys.version,
        "后端文件存在：" + str(os.path.isfile(SCRIPT)),
        "数据目录存在：" + str(os.path.isdir(DATA_DIR)),
        "",
        "========== 网络命名空间 =========="
    ]

    try:
        with open("/proc/self/ns/net") as f:
            result.append("Gunicorn：" + f.read().strip())
    except Exception as e:
        result.append("Gunicorn 命名空间读取失败：" + repr(e))

    if pid:
        try:
            with open("/proc/%d/ns/net" % pid) as f:
                result.append("IPTV 后端：" + f.read().strip())
        except Exception as e:
            result.append("后端命名空间读取失败：" + repr(e))
    else:
        result.append("IPTV 后端：没有有效后端 PID")

    result.extend([
        "",
        "========== TCP 8768 原始状态 ==========",
        read_tcp_table(),
        "",
        "========== Socket 所属进程 ==========",
        check_socket_owner(pid),
        "",
        "========== 连接测试 =========="
    ])

    for host in ("127.0.0.1", "localhost"):
        try:
            with socket.create_connection(
                (host, BACKEND_PORT), timeout=3
            ):
                result.append(host + "：连接成功")
        except Exception as e:
            result.append(
                host + "：失败 " + type(e).__name__ +
                " errno=" + str(getattr(e, "errno", None)) +
                " " + str(e)
            )

    result.extend([
        "",
        "========== 独立 Python 子进程测试 ==========",
        run_command([
            sys.executable,
            "-c",
            "import socket; s=socket.socket(); "
            "s.settimeout(3); "
            "print('connect_ex结果:', "
            "s.connect_ex(('127.0.0.1',8768)))"
        ]),
        "",
        "========== HTTP 健康检查 =========="
    ])

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8768/health", timeout=4
        ) as response:
            result.append("HTTP 状态：" + str(response.status))
            result.append(response.read(2000).decode(
                "utf-8", "replace"
            ))
    except Exception as e:
        result.append(type(e).__name__ + "：" + str(e))

    result.extend([
        "",
        "========== 后端进程 ==========",
        get_process_info(pid),
        "",
        "========== 后端日志 ==========",
        read_backend_log(),
        "",
        firewall_diagnostics()
    ])

    return "\n".join(result)


def start_backend():
    if not os.path.isfile(SCRIPT):
        raise RuntimeError("后端文件不存在：" + SCRIPT)

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if port_ready():
            return

        pid = get_backend_pid()

        if pid:
            for _ in range(8):
                time.sleep(1)
                if port_ready():
                    return

            raise RuntimeError(
                "发现 IPTV 后端进程，但 8768 端口无法连接。\n\n"
                + diagnostic()
            )

        try:
            os.remove(PID_FILE)
        except Exception:
            pass

        env = os.environ.copy()
        env["YSP_DATA_DIR"] = DATA_DIR

        with open(LOG_FILE, "a", buffering=1) as log:
            log.write(
                "\n\n========== 新的后端启动尝试 %s ==========\n"
                % time.strftime("%Y-%m-%d %H:%M:%S")
            )

            process = subprocess.Popen(
                [
                    sys.executable,
                    SCRIPT,
                    str(BACKEND_PORT),
                    "--bind",
                    BACKEND_HOST,
                    "--no-engine"
                ],
                cwd=BASE_DIR,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True
            )

        with open(PID_FILE, "w") as f:
            f.write(str(process.pid))

        for _ in range(25):
            time.sleep(1)

            if port_ready():
                return

            if process.poll() is not None:
                break

        raise RuntimeError(
            "IPTV 后端启动后无法连接 8768 端口。\n\n"
            + diagnostic()
        )


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")

    if path == "/__diag":
        from urllib.parse import parse_qs

        supplied = parse_qs(query).get("key", [""])[0]

        if supplied != DIAG_KEY:
            body = "403 Forbidden"
            start_response("403 Forbidden", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body.encode())))
            ])
            return [body.encode()]

        body = diagnostic().encode("utf-8", "replace")
        start_response("200 OK", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body)))
        ])
        return [body]

    try:
        start_backend()

        method = environ.get("REQUEST_METHOD", "GET")
        body_length = environ.get("CONTENT_LENGTH", "")
        request_body = None

        if body_length:
            request_body = environ["wsgi.input"].read(
                int(body_length)
            )

        target = "http://127.0.0.1:%d%s" % (
            BACKEND_PORT, path
        )

        if query:
            target += "?" + query

        headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[5:].replace("_", "-").title()
                if name.lower() not in (
                    "host", "connection", "content-length"
                ):
                    headers[name] = value

        content_type = environ.get("CONTENT_TYPE")
        if content_type:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(
            target,
            data=request_body,
            headers=headers,
            method=method
        )

        try:
            response = urllib.request.urlopen(
                request, timeout=120
            )
        except urllib.error.HTTPError as e:
            response = e

        response_body = response.read()
        status = "%d %s" % (
            response.code,
            response.reason
        )

        response_headers = [
            (k, v) for k, v in response.headers.items()
            if k.lower() not in (
                "transfer-encoding", "connection"
            )
        ]

        start_response(status, response_headers)
        return [response_body]

    except Exception as e:
        body = (
            "IPTV 后端启动或连接失败：\n\n"
            + repr(e)
        ).encode("utf-8", "replace")

        start_response("503 Service Unavailable", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body)))
        ])
        return [body]


app = application
