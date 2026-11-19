"""데모·시나리오용 합성 라벨 이미지와 OCR 결과 녹화(sidecar)를 data/samples/ 에 만든다.

실제 제품 라벨 사진은 저장소에 넣지 않는다. 여기서 만드는 이미지는 글자를 그린 합성 이미지이고,
<이미지>.ocr.json 은 그 이미지에 대한 OCR 결과를 흉내 낸 녹화본이다(OCR_ENGINE=sidecar 가 읽는다).
실행: .venv/bin/python scripts/make_samples.py
"""
import json
import os

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "samples")
SAMPLES = {
    "vitc.png": (["Supplement Facts", "Vitamin C (as Ascorbic Acid) 1000 mg",
                  "Other ingredients: Cellulose, Magnesium stearate"], None, [0.95, 0.93, 0.91], 0),
    "garcinia.png": (["Supplement Facts", "Garcinia gummi-gutta fruit extract 500 mg",
                      "Chromium (as picolinate) 200 mcg", "Other ingredients: Gelatin"], None, [0.94, 0.9, 0.92, 0.93], 0),
    "preworkout.png": (["Supplement Facts", "Caffeine Anhydrous 200 mg", "Beta-Alanine 2 g",
                        "Pelargonium graveolens stem extract 40 mg", "Pausinystalia johimbe bark extract 3 mg",
                        "Other ingredients: Citric acid, Natural flavors"], None, [0.95, 0.93, 0.92, 0.9, 0.91, 0.93], 0),
    "blurry.png": (["Supplement Facts", "Garcinia 500 mg", "Other ingredients"],
                   ["Supp1em?nt F@cts", "G?rc..ia 5?0 mg", "0th?r ingr..."], [0.35, 0.22, 0.30], 4),
    "blurry2.png": (["Supplement Facts", "Garcinia 500 mg", "Other ingredients"],
                    ["Suppl?ment Fa..s", "Garc?nia 50? m?", "Oth?r ingred..."], [0.42, 0.31, 0.38], 3),
}


DSLD = {  # T3 외부 라벨 검색 오프라인 사본(합성). match 문자열이 상품명에 들어 있으면 찾은 것으로 본다
    "sample-fatburner.json": {"match": "SAMPLE Fat Burner", "title": "SAMPLE Fat Burner Capsules (합성 DSLD 사본)",
                              "lines": ["Supplement Facts", "Caffeine Anhydrous 200 mg",
                                        "Pausinystalia johimbe bark extract 5 mg", "Other ingredients: Cellulose"]},
}


MARKET = {  # 국내 판매 검색 합성 사본(실제 판매 데이터 아님). match 문자열이 검색어에 들어 있으면 사용
    "sample-vitamin-c.json": {"match": "SAMPLE Vitamin C", "items": [
        {"title": "SAMPLE 비타민C 1000mg 120정 해외직구", "mall": "합성상점A", "price": 18900, "brand": "SAMPLE"},
        {"title": "[SAMPLE] Vitamin C 1000 mg 120 Tablets 구매대행", "mall": "합성상점B", "price": 21500, "brand": "SAMPLE"},
        {"title": "SAMPLE 비타민C 1000mg 250정 대용량", "mall": "합성상점C", "price": 32000, "brand": "SAMPLE"},
        {"title": "SAMPLE 비타민 C 1000mg 120정 2개 세트", "mall": "합성상점D", "price": 36000, "brand": "SAMPLE"},
        {"title": "다른브랜드 비타민C 1000mg 120정", "mall": "합성상점E", "price": 9900, "brand": "OTHER"},
        {"title": "SAMPLE 츄어블 비타민", "mall": "합성상점F", "price": 12000, "brand": "SAMPLE"}]},
    "sample-preworkout.json": {"match": "SAMPLE Pre-Workout", "items": [
        {"title": "SAMPLE 프리워크아웃 파우더 30회분 Pre-Workout", "mall": "합성상점G", "price": 45000, "brand": "SAMPLE"},
        {"title": "SAMPLE Pre-Workout Powder 30 Servings 해외배송", "mall": "합성상점H", "price": 52000, "brand": "SAMPLE"},
        {"title": "다른브랜드 부스터 프리워크아웃", "mall": "합성상점I", "price": 39000, "brand": "OTHER"}]},
}


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.join(OUT, "dsld"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "market"), exist_ok=True)
    for name, rec in MARKET.items():
        json.dump(rec, open(os.path.join(OUT, "market", name), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for name, rec in DSLD.items():
        json.dump(rec, open(os.path.join(OUT, "dsld", name), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for name, (lines, ocr_lines, conf, blur) in SAMPLES.items():
        im = Image.new("RGB", (640, 420), "white")
        d = ImageDraw.Draw(im)
        for i, t in enumerate(lines):
            d.text((30, 30 + i * 40), t, fill="black")
        if blur:
            im = im.filter(ImageFilter.GaussianBlur(blur))
        im.save(os.path.join(OUT, name))
        json.dump({"_note": "합성 이미지에 대한 OCR 결과 녹화(sidecar). 시나리오 재현용",
                   "lines": [{"text": t, "conf": c} for t, c in zip(ocr_lines or lines, conf)]},
                  open(os.path.join(OUT, name + ".ocr.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", OUT, sorted(SAMPLES))


if __name__ == "__main__":
    main()
