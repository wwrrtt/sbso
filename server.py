#!/usr/bin/env python3
"""test.py — Python + libsingbox.so 方案（Go c-shared）。

注意：Python ctypes 的 c_char_p 和 Go C.CString() 的 ABI 不兼容。
必须使用 c_void_p 保持原始指针，详见 _get_error() 方法。
"""

import sys, os, json, threading, signal, ctypes, urllib.request, tempfile

sys.path.insert(0, ".")

# ═══ .so 下载地址 ═════════════════════════

SO_URL = "https://github.com/lostwwrrtt/sbso/releases/download/v1.0/libsingbox.so"   # ← 改为你的实际下载地址

# ═══════════════════════════════════════════════
# SingBox 封装（修复版）
# ═══════════════════════════════════════════════

class SingBoxError(Exception):
    pass

class SingBox:
    def __init__(self, lib):
        """lib: 已加载的 ctypes.CDLL 对象"""
        self._lib = lib
        self._lock = threading.Lock()

        # ⚠️ 关键修复：用 c_void_p 而非 c_char_p
        # c_char_p 会让 ctypes 自动拷贝字符串，丢失原始指针 → free() 崩溃
        self._lib.singbox_start.argtypes = [ctypes.c_char_p]
        self._lib.singbox_start.restype  = ctypes.c_int
        self._lib.singbox_stop.argtypes  = []
        self._lib.singbox_stop.restype   = ctypes.c_int
        self._lib.singbox_get_error.argtypes = []
        self._lib.singbox_get_error.restype  = ctypes.c_void_p
        self._lib.singbox_free_string.argtypes = [ctypes.c_void_p]
        self._lib.singbox_free_string.restype  = None
        self._lib.singbox_is_running.argtypes  = []
        self._lib.singbox_is_running.restype   = ctypes.c_int

    def start(self, config):
        if isinstance(config, dict):
            config = json.dumps(config, ensure_ascii=False)
        with self._lock:
            rc = self._lib.singbox_start(config.encode("utf-8"))
            if rc != 0:
                err = self._get_error()
                raise SingBoxError(err)

    def stop(self):
        with self._lock:
            rc = self._lib.singbox_stop()
            if rc != 0:
                err = self._get_error()
                if err and err != "no running instance":
                    raise SingBoxError(err)

    @property
    def running(self):
        with self._lock:
            return bool(self._lib.singbox_is_running())

    def _get_error(self):
        """从原始指针读取错误字符串，避免 c_char_p 自动拷贝问题。"""
        ptr = self._lib.singbox_get_error()
        if ptr is None or ptr == 0:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self._lib.singbox_free_string(ptr)

    def __enter__(self): return self
    def __exit__(self, *args): self.stop()


# ═══ 配置 ══════════════════════════════════

CF_TOKEN       = os.environ.get("CF_TOKEN",       "xxx")
VMESS_UUID     = os.environ.get("VMESS_UUID",     "xxx")
VMESS_PORT     = int(os.environ.get("VMESS_PORT",     "xxx"))
VMESS_PATH     = os.environ.get("VMESS_PATH",     "/xxx")
HA_CONNECTIONS = int(os.environ.get("HA_CONNECTIONS", "0"))

config = config = {
    "log": {"disabled": True},
    "inbounds": [
        # ── Cloudflare Tunnel ──
        {"type": "cloudflared", "tag": "cf-tunnel-in",
         "token": CF_TOKEN,
         "protocol": "quic",               # ← 改成 http2，避开 UDP QoS，更稳定
         "ha_connections": 0,                # ← 2 条连接足够，4 条反而容易触发限速
         "edge_ip_version": 0,
         "grace_period": "30s"},             # ← 优雅关闭窗口
        # ── vmess+ws（只监听本地，通过隧道暴露）──
        {"type": "vmess", "tag": "vmess-ws-in",
         "listen": "0.0.0.0",
         "listen_port": 44344,               # ← 改成你指定的端口
         "users": [{"uuid": VMESS_UUID, "alterId": 0}],
         "transport": {"type": "ws", "path": VMESS_PATH}},
    ],
    "outbounds": [
        {"type": "direct"}    
    ],
}

# ═══ Web 服务 ═════════════════════════════

INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.html")
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if os.path.isfile(INDEX_PATH):
            with open(INDEX_PATH, "rb") as f: body = f.read()
            ct = "text/html; charset=utf-8"
        else:
            body = b"hello world"
            ct = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args): pass

# ═══ 主入口 ═══════════════════════════════

_web_server: HTTPServer | None = None
stop_event = threading.Event()

def handle_signal(sig, _frame):
    print(f"\n[main] 收到信号 {sig}，正在停止...")
    stop_event.set()
    if _web_server:
        threading.Thread(target=_web_server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT,  handle_signal)

def main():
    global _web_server

    # ── 1. 下载 .so 到临时路径 ──
    print(f"[server] 获取 ing  ...")
    fd, so_path = tempfile.mkstemp(suffix=".so", prefix="server_")
    os.close(fd)
    urllib.request.urlretrieve(SO_URL, so_path)
    print("[server] ✅ 获取完成")

    # ── 2. 加载 .so ──
    print("[server] 加载 ing ...")
    lib = ctypes.cdll.LoadLibrary(so_path)
    os.unlink(so_path)  # ✅ 删文件，内存不受影响
    sb = SingBox(lib)
    print("[server] ✅  加载成功")

    # ── 3. 在主线程启动 sing-box ──
    print("[server] 启动...")
    try:
        sb.start(config)
        print(f"[server] ✅ 运行中 (running={sb.running})")
    except SingBoxError as e:
        print(f"[server] ❌ 启动失败: {e}")
        return

    # ── 4. 再启 Web 线程 ──
    _web_server = HTTPServer(("0.0.0.0", 5000), Handler)
    web_thread = threading.Thread(target=lambda s: s.serve_forever(), args=(_web_server,), daemon=True)
    web_thread.start()
    print(f"[web] ✅ 监听 http://0.0.0.0:5000")

    # ── 5. 等待停止信号 ──
    try:
        stop_event.wait()
    except Exception as e:
        print(f"[main] 意外退出: {e}")

    sb.stop()
    web_thread.join(timeout=2)
    print("[main] ✅ 全部服务已停止")

if __name__ == "__main__":
    main()
