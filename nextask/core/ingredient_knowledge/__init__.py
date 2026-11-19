"""Version-bound candidate retrieval; reference names/common hints never authorize a verdict."""
import hashlib
import os
from functools import lru_cache

from .search import HERE, IngredientKB


@lru_cache(maxsize=1)
def _load():
    index = IngredientKB.load()
    digest = hashlib.sha256()
    for name in ("search.py", "kb.jsonl", "common.jsonl"):
        digest.update((HERE / name).read_bytes())
    return index, digest.hexdigest()


@lru_cache(maxsize=4)
def _compatible(registry):
    index, _ = _load()
    fields = ("kr", "en", "etc", "appn_dt", "basis", "notice_no")
    return (not registry.is_sample and set(registry.by_id) == set(index.entries)
            and all(all(getattr(entry, key) == index.entries[entry.id].get(key, [] if key == "etc" else "")
                        for key in fields) for entry in registry.entries))


def retrieve(registry, label):
    mode = os.environ.get("NEXTASK_INGREDIENT_KB", "0")
    if mode not in ("1", "shadow"):
        return None
    index, version = _load()
    base = {"version": version, "query": label, "mode": "candidates" if mode == "1" else "shadow",
            "authority": "candidate_only", "registry": registry.source}
    if not _compatible(registry):
        return {**base, "status": "registry_mismatch", "candidates": [], "common_hint": None}
    if not isinstance(label, str) or len(label) > 1024:
        return {**base, "status": "query_unavailable", "candidates": [], "common_hint": None}
    return {**base, "status": "retrieved", **index.search(label)}
