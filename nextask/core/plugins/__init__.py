import importlib
import os

from .. import config  # noqa: F401  (.env 를 먼저 읽는다)


def load(name=None):
    """NEXTASK_PLUGIN=supplement (이 폴더의 파일 이름) 또는 패키지 경로 a.b.c"""
    name = name or os.environ.get("NEXTASK_PLUGIN") or "supplement"
    mod = importlib.import_module(name if "." in name else f"{__name__}.{name}")
    return mod.PLUGIN
