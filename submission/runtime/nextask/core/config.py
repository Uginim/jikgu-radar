"""app/.env 읽기(비밀 키). 값은 로그·표준 출력에 찍지 않는다. 이미 환경 변수에 있으면 덮어쓰지 않는다."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRET_NAMES = ("DATA_GO_KR_KEY", "MFDS_API_KEY", "ZORO_API_KEY", "NAVER_CLIENT_SECRET")


def load_dotenv(path=None):
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v


def redact(text):
    """문자열에 섞인 비밀 값을 가린다(오류 메시지에 URL 째로 들어가는 경우 등)."""
    s = str(text)
    for k in SECRET_NAMES:
        v = os.environ.get(k)
        if v and len(v) > 6:
            s = s.replace(v, "***")
            from urllib.parse import quote
            s = s.replace(quote(v, safe=""), "***")
    return s


load_dotenv()
