#!/usr/bin/env python3
"""
src/ 의 소스 파일을 k8s/ 의 ConfigMap YAML 로 변환합니다.
  python3 tools/gen_configmaps.py      (학번 폴더에서 실행)

src/index.html, src/server.py   → k8s/configmap-app.yaml     (chaeeon-cv-app)
src/profile.json                → k8s/configmap-profile.yaml (chaeeon-cv-profile)

oc 가 있다면 아래와 같이 해도 결과는 같습니다.
  oc create configmap chaeeon-cv-app --from-file=src/index.html --from-file=src/server.py \
     --dry-run=client -o yaml
"""
from pathlib import Path
import base64

ROOT = Path(__file__).resolve().parent.parent
SRC, K8S = ROOT / "src", ROOT / "k8s"
LABELS = "    app: chaeeon-cv\n    app.kubernetes.io/name: chaeeon-cv\n    app.kubernetes.io/part-of: chaeeon-portfolio\n"


def block(key: str, text: str) -> str:
    body = "".join("    " + ln if ln.strip() else "\n" for ln in text.rstrip("\n").splitlines(keepends=True))
    if not body.endswith("\n"):
        body += "\n"
    return f"  {key}: |\n{body}"


def write(path: Path, header: str, data: str):
    path.write_text(header + "data:\n" + data, encoding="utf-8")
    print("wrote", path.relative_to(ROOT))


write(
    K8S / "configmap-app.yaml",
    "# 애플리케이션 코드 ConfigMap — src/ 에서 tools/gen_configmaps.py 로 생성\n"
    "# Deployment 에서 /opt/app-root/app 에 볼륨으로 마운트됩니다.\n"
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: chaeeon-cv-app\n  labels:\n" + LABELS,
    block("server.py", (SRC / "server.py").read_text(encoding="utf-8"))
    + block("index.html", (SRC / "index.html").read_text(encoding="utf-8"))
    + "".join(block(name + ".b64", base64.b64encode((SRC / "assets" / name).read_bytes()).decode())
              for name in ("killingpart.jpg", "alarmclock.jpg"))
    + block("compose.svg", (SRC / "assets/compose.svg").read_text(encoding="utf-8")),
)

write(
    K8S / "configmap-profile.yaml",
    "# 포트폴리오 내용 + 화면 설정 ConfigMap\n"
    "#  - profile.json   : /opt/app-root/profile 에 파일로 마운트 (수정 시 재배포 없이 약 1분 내 반영)\n"
    "#  - WELCOME_MESSAGE, DEFAULT_THEME : 환경변수로 주입 (수정 후 oc rollout restart 필요)\n"
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: chaeeon-cv-profile\n  labels:\n" + LABELS,
    '  WELCOME_MESSAGE: "OpenShift Pod에서 실행되는 터미널형 포트폴리오입니다."\n'
    '  DEFAULT_THEME: "sim"\n'
    + block("profile.json", (SRC / "profile.json").read_text(encoding="utf-8")),
)
