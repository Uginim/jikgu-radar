"""OCR 교체 가능 인터페이스. Zoro-1 은 448px 입력이라 라벨의 작은 글씨를 직접 읽게 하지 않는다.

    ocr = get_ocr()                 # OCR_ENGINE=sidecar | remote(판정 서버 GravityOCR-KO) | tesseract | <패키지.모듈:클래스>
    r = ocr.read(image_path)        # -> {"engine", "lines": [{"text", "conf"}], "mean_conf"}

새 엔진은 read(image_path) 만 구현하면 된다 (예: 클라우드 OCR, PaddleOCR, macOS Vision).
"""
import glob
import hashlib
import importlib
import json
import os
import shutil
import subprocess


class OCRUnavailable(RuntimeError):
    """Configured OCR cannot run; this is not an image-quality observation."""


class OCRFailed(RuntimeError):
    """OCR engines failed after execution; preserves observed HTTP attempts."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


class OCR:
    name = "base"

    def read(self, image_path):
        raise NotImplementedError

    @staticmethod
    def _pack(engine, lines):
        confs = [ln["conf"] for ln in lines if ln.get("conf") is not None]
        return {"engine": engine, "lines": lines, "mean_conf": round(sum(confs) / len(confs), 3) if confs else 0.0}


class SidecarOCR(OCR):
    """사진 옆의 <사진>.ocr.json ({"lines":[{"text","conf"}]}) 을 읽는다. 데모·시나리오 재현용(OCR 결과 녹화)."""
    name = "sidecar"

    def __init__(self, dirs=None):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.dirs = dirs or os.environ.get("OCR_SIDECAR_DIRS", os.path.join(root, "data", "samples")).split(",")

    @staticmethod
    def _sha(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def read(self, image_path):
        p = image_path + ".ocr.json"
        if not os.path.exists(p):   # 업로드로 경로가 바뀐 같은 사진: 내용 해시로 찾는다
            sha = self._sha(image_path)
            p = next((sc for d in self.dirs for sc in glob.glob(os.path.join(d, "**", "*.ocr.json"), recursive=True)
                      if os.path.exists(sc[:-9]) and self._sha(sc[:-9]) == sha), None)
            if not p:
                raise OCRUnavailable("OCR 녹화(sidecar) 없음 — 이미지 판독을 실행하지 않음")
        return self._pack(self.name, json.load(open(p, encoding="utf-8"))["lines"])


class TesseractOCR(OCR):
    """tesseract CLI (brew install tesseract tesseract-lang). 줄 단위 신뢰도는 단어 신뢰도 평균(0~1)."""
    name = "tesseract"

    def __init__(self, lang=None):
        self.lang = lang or os.environ.get("OCR_LANG", "eng+kor")
        if not shutil.which("tesseract"):
            raise RuntimeError("tesseract not installed")

    def read(self, image_path):
        out = subprocess.run(["tesseract", image_path, "stdout", "-l", self.lang, "tsv"], capture_output=True,
                             text=True, timeout=60, check=True).stdout.splitlines()
        rows = [r.split("\t") for r in out[1:]]
        lines = {}
        for r in rows:
            if len(r) == 12 and r[11].strip() and float(r[10]) >= 0:
                k = (r[2], r[3], r[4])
                lines.setdefault(k, []).append((r[11], float(r[10]) / 100))
        return self._pack(self.name, [{"text": " ".join(w for w, _ in ws), "conf": round(sum(c for _, c in ws) / len(ws), 3)}
                                      for ws in lines.values()])


class RemoteOCR(OCR):
    """판정 서버의 OCR(GravityOCR-KO, POST /v1/ocr). ZORO_URL(S) 가 있어야 한다. 서버가 ZORO_OCR=1 이어야 함."""
    name = "remote"

    def __init__(self):
        if not (os.environ.get("ZORO_URLS") or os.environ.get("ZORO_URL")):
            raise RuntimeError("ZORO_URL not set")
        from ._infra_client import ZoroClient
        self.client = ZoroClient()
        import threading
        self._cache = {}
        self._cache_lock = threading.Lock()

    def read(self, image_path):
        import copy
        from . import caselog

        with open(image_path, "rb") as image:
            raw = image.read()
        case_id = getattr(caselog.CURRENT, "case_id", None)
        key = (case_id, hashlib.sha256(raw).hexdigest())
        cache = getattr(self, "_cache", None)
        if cache is not None and case_id:
            with self._cache_lock:
                previous = cache.get(key)
                if previous is not None:
                    result = copy.deepcopy(previous)
                    result.update(attempts=[], attempt_count=0,
                                  cache_reuse={"case_id": case_id, "image_sha256": key[1],
                                               "request_id": result.get("response", {}).get("request_id")})
                    result["orientation"] = {"reused": True, "original": result.get("orientation", {})}
                    return result
        result = self._read_oriented(raw)
        if cache is not None and case_id:
            with self._cache_lock:
                if len(cache) >= 64:
                    cache.pop(next(iter(cache)))
                cache[key] = copy.deepcopy(result)
        return result

    def _orientation(self, raw):
        import base64
        from .sources import _gallery_preview
        from ._infra_client import ZoroError, ZoroUnavailable

        mime = "png" if raw.startswith(b"\x89PNG") else "jpeg"
        try:
            preview = _gallery_preview(f"data:image/{mime};base64," + base64.b64encode(raw).decode())
            response = self.client.decide({
                "state": "Choose the rotation needed to make the SUPPLEMENT FACTS ingredient panel upright. "
                         "Focus on the ingredient panel, not the logo or warning paragraph. Angles are counterclockwise.",
                "questions": {"upright": {"type": "choice", "criteria": {
                    "upright": "The Supplement Facts heading and ingredient lines read horizontally from left to right. No rotation is needed.",
                    "sideways": "The Supplement Facts heading and ingredient lines run vertically or upside down. Rotation is needed."}},
                    "orientation": {"type": "choice", "criteria": {
                    "0": "The ingredient panel is already upright; no rotation.",
                    "90": "Rotate the image 90 degrees counterclockwise to make the ingredient panel upright.",
                    "180": "Rotate the image 180 degrees to make the ingredient panel upright.",
                    "270": "Rotate the image 270 degrees counterclockwise (90 clockwise) to make the ingredient panel upright."}}},
                "image": preview})
            answer = response["answers"]["orientation"]
            choice = answer["choice"]
            confidence = float(answer["probabilities"][choice])
            if choice not in ("0", "90", "180", "270") or not 0 <= confidence <= 1:
                raise ValueError("invalid orientation response")
            upright = response["answers"]["upright"]["choice"]
            if upright not in ("upright", "sideways"):
                raise ValueError("invalid upright response")
            return (0 if upright == "upright" else int(choice)), {"response": response, "confidence": confidence}
        except (OSError, ValueError, KeyError, TypeError, ZoroError, ZoroUnavailable) as exc:
            return 0, {"error": type(exc).__name__, "attempts": getattr(exc, "attempts", [])}

    def _read_oriented(self, raw):
        import io
        import re
        from PIL import Image, ImageOps
        from ._infra_client import ZoroError, ZoroUnavailable

        def evidence(result):
            text = "\n".join(line["text"] for line in result["lines"])
            markers = len(re.findall(r"\b(?:supplement facts|nutrition facts|other ingredients|ingredients\s*:)", text, re.I))
            doses = len(re.findall(r"\d[\d,.]*\s*(?:mg|mcg|[µμ]g|g|iu)\b", text, re.I))
            return (markers > 0, doses, markers)

        first, orientation = self._orientation(raw)
        angles = list(dict.fromkeys((first, 0, 90, 270)))[:3]
        best, rank, selected_angle = None, (False, 0, 0), 0
        attempts, candidates = [], []
        for angle in angles:
            data = raw
            if angle:
                with Image.open(io.BytesIO(raw)) as image, io.BytesIO() as buffer:
                    ImageOps.exif_transpose(image).rotate(angle, expand=True).convert("RGB").save(buffer, format="JPEG", quality=95)
                    data = buffer.getvalue()
            try:
                result = self._read_bytes(data)
            except (ZoroError, ZoroUnavailable) as exc:
                if best is None:
                    raise
                attempts.extend(exc.attempts)
                candidates.append({"angle": angle, "status": "failed", "error": type(exc).__name__})
                continue
            attempts.extend(result.get("attempts", []))
            score = evidence(result)
            candidates.append({"angle": angle, "status": "read", "evidence_score": score,
                               "line_count": len(result["lines"]), "response": result.get("response", {})})
            if best is None or score > rank:
                best, rank, selected_angle = result, score, angle
            if rank[0] and rank[1]:
                break
        best.update(attempts=attempts, attempt_count=len(attempts), orientation=orientation)
        if selected_angle or len(candidates) > 1:
            best["rotation_retry"] = {"selected_angle": selected_angle, "candidates": candidates}
        return best

    def _read_bytes(self, raw):
        import base64
        r = self.client.ocr(base64.b64encode(raw).decode())
        lines = r.get("lines") or [{"text": t, "conf": None} for t in (r.get("text") or "").splitlines() if t.strip()]
        out = self._pack(r.get("engine", self.name), [{"text": ln["text"], "conf": ln.get("conf")} for ln in lines])
        if r.get("mean_conf") is not None:
            out["mean_conf"] = r["mean_conf"]
        out["repeat_ratio"] = r.get("repeat_ratio")      # 같은 글자 반복(환각) 비율 → 판독 신뢰 판정에 씀
        out["mean_token_prob"] = r.get("mean_token_prob")
        out["response"] = {k: r[k] for k in ("text", "request_id", "tokens", "timing_ms") if k in r}
        if "attempts" in r:
            out["attempts"] = r["attempts"]
            out["attempt_count"] = len(r["attempts"])
        return out


class ChainOCR(OCR):
    """앞에서부터 시도해 처음 성공한 결과를 쓴다(예: sidecar → tesseract)."""
    def __init__(self, engines):
        self.engines = engines
        self.name = "+".join(e.name for e in engines)

    def read(self, image_path):
        errs, attempted = [], False
        attempts = []
        for e in self.engines:
            try:
                result = e.read(image_path)
                attempts.extend(result.get("attempts", []))
                if attempts:
                    result.update(attempts=attempts, attempt_count=len(attempts))
                return result
            except OCRUnavailable as ex:
                errs.append(f"{e.name}: {ex}")
            except Exception as ex:  # noqa: BLE001
                attempts.extend(getattr(ex, "attempts", []))
                attempted = True
                errs.append(f"{e.name}: {ex}")
        message = "; ".join(errs) or "사용 가능한 OCR 엔진 없음"
        if not attempted:
            raise OCRUnavailable(message)
        raise OCRFailed(message, attempts)


def get_ocr(spec=None):
    spec = spec or os.environ.get("OCR_ENGINE", "sidecar,remote,tesseract")
    engines = []
    for s in spec.split(","):
        s = s.strip()
        try:
            if s == "sidecar":
                engines.append(SidecarOCR())
            elif s == "remote":
                engines.append(RemoteOCR())
            elif s == "tesseract":
                engines.append(TesseractOCR())
            elif ":" in s:
                mod, cls = s.split(":")
                engines.append(getattr(importlib.import_module(mod), cls)())
        except Exception:  # noqa: BLE001 -- 설치 안 된 엔진은 건너뛴다
            continue
    return ChainOCR(engines)
