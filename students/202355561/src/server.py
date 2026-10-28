"""
chaeeon-cv : 터미널형 포트폴리오 웹 서버
- 표준 라이브러리만 사용 (pip 설치/이미지 빌드 불필요)
- ConfigMap 으로 주입된 index.html / profile.json 을 서빙
- Downward API 로 주입된 Pod 정보를 /api/info 로 노출
- Secret 으로 주입된 비밀번호로 /api/sudo 검증
- /healthz (Liveness), /readyz (Readiness) 제공
"""
import hmac
import json
import os
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
APP_DIR = Path(os.environ.get("APP_DIR", "/opt/app-root/app"))          # ConfigMap: chaeeon-cv-app
PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", "/opt/app-root/profile"))  # ConfigMap: chaeeon-cv-profile

START_TIME = time.time()
STATS = {"requests": 0}
STATS_LOCK = threading.Lock()

# /api/sudo 무차별 대입 방지: IP 당 1분에 5회
SUDO_LIMIT, SUDO_WINDOW = 5, 60
SUDO_ATTEMPTS: dict[str, list[float]] = {}
SUDO_LOCK = threading.Lock()


def pod_info() -> dict:
    up = int(time.time() - START_TIME)
    return {
        "podName": os.environ.get("POD_NAME", platform.node()),
        "namespace": os.environ.get("POD_NAMESPACE", "local"),
        "nodeName": os.environ.get("NODE_NAME", "localhost"),
        "podIP": os.environ.get("POD_IP", "127.0.0.1"),
        "memoryLimit": os.environ.get("MEMORY_LIMIT", "-"),
        "cpuLimit": os.environ.get("CPU_LIMIT", "-"),
        "uptimeSeconds": up,
        "requestsServed": STATS["requests"],
        "python": platform.python_version(),
        "os": platform.platform(terse=True),
        "welcome": os.environ.get("WELCOME_MESSAGE", ""),
        "defaultTheme": os.environ.get("DEFAULT_THEME", "sim"),
        "sudoEnabled": bool(os.environ.get("SUDO_PASSWORD")),
    }


def ready() -> bool:
    return (APP_DIR / "index.html").is_file() and (PROFILE_DIR / "profile.json").is_file()


def sudo_allowed(ip: str) -> bool:
    now = time.time()
    with SUDO_LOCK:
        hits = [t for t in SUDO_ATTEMPTS.get(ip, []) if now - t < SUDO_WINDOW]
        if len(hits) >= SUDO_LIMIT:
            SUDO_ATTEMPTS[ip] = hits
            return False
        hits.append(now)
        SUDO_ATTEMPTS[ip] = hits
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "chaeeon-cv/1.0"
    sys_version = ""

    # ---------- helpers ----------
    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Served-By", os.environ.get("POD_NAME", platform.node()))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _client_ip(self) -> str:
        # OpenShift 라우터는 실제 클라이언트 IP 를 X-Forwarded-For 의 '마지막'에 덧붙임
        # (앞쪽 값은 클라이언트가 위조할 수 있으므로 사용하지 않음)
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def log_message(self, fmt, *args):
        # oc logs 로 확인할 수 있도록 stdout 에 접근 로그 출력 (헬스체크는 생략)
        if self.path in ("/healthz", "/readyz"):
            return
        sys.stdout.write("%s %s %s\n" % (self.log_date_time_string(), self._client_ip(), fmt % args))
        sys.stdout.flush()

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/healthz", "/readyz"):
            with STATS_LOCK:
                STATS["requests"] += 1

        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path == "/readyz":
            return self._json(200, {"status": "ready"}) if ready() else self._json(503, {"status": "not ready"})
        if path in ("/", "/index.html"):
            f = APP_DIR / "index.html"
            if not f.is_file():
                return self._json(503, {"error": "index.html not mounted"})
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/profile":
            f = PROFILE_DIR / "profile.json"
            if not f.is_file():
                return self._json(503, {"error": "profile.json not mounted"})
            # 매 요청마다 다시 읽음 → ConfigMap 수정 시 재배포 없이 반영
            return self._send(200, f.read_bytes(), "application/json; charset=utf-8")
        if path == "/api/info":
            return self._json(200, pod_info())
        return self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/sudo":
            return self._json(404, {"error": "not found"})
        with STATS_LOCK:
            STATS["requests"] += 1

        secret = os.environ.get("SUDO_PASSWORD", "")
        if not secret:
            return self._json(503, {"ok": False, "error": "sudo is not configured"})
        if not sudo_allowed(self._client_ip()):
            return self._json(429, {"ok": False, "error": "too many attempts, try again in a minute"})

        try:
            length = min(int(self.headers.get("Content-Length", "0")), 1024)
            data = json.loads(self.rfile.read(length) or b"{}")
            given = str(data.get("password", ""))
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "bad request"})

        # 타이밍 공격 방지를 위해 상수 시간 비교
        if hmac.compare_digest(given.encode(), secret.encode()):
            return self._json(200, {"ok": True, "message": os.environ.get("SUDO_MESSAGE", "Access granted.")})
        return self._json(401, {"ok": False, "error": "Sorry, try again."})


def main():
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"chaeeon-cv listening on :{PORT} (pod={os.environ.get('POD_NAME', platform.node())})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
