"""One read-only fleet observation; invoked by the existing 600-second watcher.

Outputs (under infra/logs, or --output-dir): fleet-usage-latest.json,
fleet-usage-history.jsonl (144 observations), fleet-usage-requests.jsonl
(10,000 metadata rows), and fleet-usage.lock. No inference, remote writes,
recovery, timers, credentials, or request/response text are collected.

Offline entry point: --fixture PATH --output-dir DIR. Fixture shape:
{"at": "ISO timestamp", "provider": {"available": true, "statuses":
{"zoro2": "RUNNING", "zoro3": "STOPPED", "zoro4": null},
"running_count": 1}, "hosts": {"zoro2": {"available": true,
"uptime_s": 123, "gpu_models": ["NVIDIA L4"], "rows": [usage_v1_log],
"logs": {"available": true, "truncated": false, "invalid_rows": 0}}}}.
Fixtures never contact SSH/provider. Every persisted request is allowlisted.
Cost bounds are runtime estimates, not invoices or measured billing intervals.
"""
import argparse
from collections import Counter, deque
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.request import urlopen

HOSTS = ("zoro2", "zoro3", "zoro4", "zoro5")
STARTS = {
    "zoro2": ("2026-11-19T19:41:00+09:00", "2026-11-19T19:41:00+09:00"),
    "zoro3": ("2026-11-19T21:33:51+09:00", "2026-11-19T21:35:39+09:00"),
    "zoro4": ("2026-11-19T21:33:51+09:00", "2026-11-19T21:35:39+09:00"),
    "zoro5": ("2026-11-19T22:30:30+09:00", "2026-11-19T22:30:43+09:00"),
}
DEADLINE = "2026-11-20T14:00:00+09:00"
RATES = {"zoro2": 0.97, "zoro3": 0.97, "zoro4": 0.97, "zoro5": 2.2332}
MAX_RUNNING = 3
MAX_ROWS = 10000
REMOTE_ROWS = 5000
SCAN_BYTES = 16 * 1024 * 1024
USAGE = ("input_tokens", "output_tokens", "reasoning_tokens", "image_count", "image_bytes")
TIMINGS = ("queue_ms", "compute_ms", "total_ms")
ATTRIBUTION = ("owner", "task", "case_id", "run_id")
ROUTES = ("/v1/systemone", "/v1/decide", "/v1/generate", "/v1/ocr")


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:@/+\-]{1,128}", value) else None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def seconds(value):
    return datetime.fromisoformat(value).timestamp()


def mapping(value):
    return value if isinstance(value, dict) else {}


def metadata(record, host, key):
    """Run this whitelist remotely, BEFORE any request data crosses SSH."""
    d = mapping(record)
    output = mapping(d.get("output"))
    rid = identifier(d.get("request_id"))
    attr = mapping(d.get("attribution"))
    attribution = {k: identifier(attr.get(k)) for k in ATTRIBUTION}
    if not attribution["owner"]:
        attribution["owner"] = next((p for p in ("app", "research", "infra")
                                      if rid and rid.startswith(p + "-")), "unattributed")
    version = "usage-v1" if d.get("telemetry_version") == "usage-v1" else "legacy"
    usage: dict[str, int | float | None] = {k: number(mapping(d.get("usage")).get(k)) for k in USAGE}
    sources = {k: "usage-v1" if version == "usage-v1" and usage[k] is not None else None for k in USAGE}
    if version == "legacy":
        # Never infer decision-head tokens, image absence, or input length.
        usage = {k: None for k in USAGE}
        usage["output_tokens"] = number(output.get("tokens"))
        usage["reasoning_tokens"] = number(mapping(output.get("reasoning_stats")).get("completion_tokens"))
        body = mapping(d.get("input"))
        image = mapping(mapping(body.get("request", body)).get("image"))
        if image and any(k in image for k in ("sha256", "sha256_b64", "bytes", "b64_chars")):
            usage["image_count"] = 1
            usage["image_bytes"] = number(image.get("bytes"))
        sources = {k: "legacy-observed" if usage[k] is not None else None for k in USAGE}
        # Already-whitelisted remote rows retain their observed legacy counts.
        if d.get("metadata_version") == "fleet-request-v1":
            usage = {k: number(mapping(d.get("usage")).get(k)) for k in USAGE}
            sources = {k: "legacy-observed" if usage[k] is not None else None for k in USAGE}
    cut = output.get("cut", d.get("cut"))
    cut = cut if cut in ("length", "empty") else "other" if cut else None
    source = output.get("answers_source", d.get("answers_source"))
    status = d.get("status")
    sha = d.get("code_sha256")
    row = {
        "metadata_version": "fleet-request-v1", "key": key, "host": host, "instance": host,
        "ts": timestamp(d.get("ts")), "request_id": rid,
        "server": identifier(d.get("server")), "revision": identifier(d.get("revision")),
        "code_sha256": sha if isinstance(sha, str) and re.fullmatch(r"[a-fA-F0-9]{64}", sha) else None,
        "telemetry_version": version, "attribution": attribution,
        "route": d.get("route") if d.get("route") in ROUTES else None,
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "usage": usage, "usage_sources": sources,
        "cut": cut, "answers_source": source if source in ("direct", "reasoned") else None,
    }
    row.update({k: number(d.get(k)) for k in TIMINGS})
    return row


def remote_observation():
    root = Path.home() / "infra"
    result = {"available": True, "uptime_s": None, "gpu_models": None,
              "health": None, "code_sha256": None, "rows": []}
    try:
        result["uptime_s"] = number(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        models = []
        for path in Path("/proc/driver/nvidia/gpus").glob("*/information"):
            for line in path.read_text().splitlines():
                if line.startswith("Model:"):
                    models.append(line.split(":", 1)[1].strip()[:128])
        result["gpu_models"] = models or None
    except OSError:
        pass
    try:
        result["code_sha256"] = hashlib.sha256((root / "server.py").read_bytes()).hexdigest()
    except OSError:
        pass
    try:
        try:
            response = urlopen("http://127.0.0.1:30001/health", timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            health = mapping(json.load(response))
        result["health"] = {
            "status": health.get("status") if health.get("status") in ("ok", "loading") else None,
            "revision": identifier(health.get("revision")),
            "server": identifier(health.get("server")),
            "code_sha256": identifier(health.get("code_sha256")),
            "telemetry_version": "usage-v1" if health.get("telemetry_version") == "usage-v1" else None,
            "uptime_s": number(health.get("uptime_s")),
            "served": number(health.get("served")), "errors": number(health.get("errors")),
            "busy": health.get("busy") if type(health.get("busy")) is bool else None,
        }
    except Exception:
        pass
    rows = deque(maxlen=REMOTE_ROWS)
    logs = {"available": (root / "logs").is_dir(), "truncated": False, "invalid_rows": 0,
            "files_scanned": 0, "rows_scanned": 0, "scan_byte_limit": SCAN_BYTES}
    budget = SCAN_BYTES
    # Newest files first; the final timestamp sort restores chronological order.
    files = sorted((root / "logs").glob("requests-*.jsonl"), reverse=True)
    for path in files:
        if budget <= 0 or len(rows) >= REMOTE_ROWS:
            logs["truncated"] = True
            break
        try:
            with path.open("rb") as stream:
                stat = path.stat()
                start = max(0, stat.st_size - budget)
                logs["truncated"] |= start > 0
                stream.seek(start)
                if start:
                    stream.readline()  # Discard incomplete leading JSON.
                chunk = deque(maxlen=REMOTE_ROWS - len(rows))
                while stream.tell() < stat.st_size:
                    offset = stream.tell()
                    line = stream.readline(min(SCAN_BYTES + 1, stat.st_size - offset))
                    budget -= len(line)
                    if not line.endswith(b"\n"):
                        logs["invalid_rows"] += 1
                        break
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise ValueError("not an object")
                        key = hashlib.sha256(f"{path.name}:{stat.st_ino}:{offset}".encode()).hexdigest()
                        chunk.append(metadata(raw, None, key))
                        logs["rows_scanned"] += 1
                    except (ValueError, UnicodeError):
                        logs["invalid_rows"] += 1
                rows.extend(chunk)
                logs["files_scanned"] += 1
        except OSError:
            logs["available"] = False
    logs["truncated"] |= logs["rows_scanned"] > len(rows)
    result["logs"] = logs
    result["rows"] = sorted(rows, key=lambda r: r["ts"] or "")
    return result


def provider_observation():
    try:
        command = subprocess.run(["/opt/homebrew/bin/brev", "ls", "--json"],
                                 capture_output=True, text=True, timeout=30)
        if command.returncode:
            raise ValueError("provider command failed")
        workspaces = json.loads(command.stdout)["workspaces"]
        if not isinstance(workspaces, list):
            raise ValueError("invalid workspaces")
        return {"available": True,
                "statuses": {h: next((identifier(w.get("status")) for w in workspaces
                                       if w.get("name") == h), None) for h in HOSTS},
                "running_count": sum(w.get("status") == "RUNNING" for w in workspaces)}
    except Exception as exc:
        return {"available": False, "statuses": {}, "running_count": None,
                "error_type": type(exc).__name__}


def live_observation():
    provider = provider_observation()
    hosts = {}
    source = Path(__file__).read_text()
    for host in HOSTS:
        if provider["statuses"].get(host) == "STOPPED":
            hosts[host] = {"available": False, "observation_reason": "provider-stopped"}
            continue
        try:
            result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                     host, "python3 - --remote"], input=source,
                                    text=True, capture_output=True, timeout=25)
            if result.returncode:
                raise ValueError("SSH command failed")
            hosts[host] = json.loads(result.stdout)
        except Exception as exc:
            hosts[host] = {"available": False, "observation_reason": type(exc).__name__}
    return {"at": datetime.now(timezone.utc).isoformat(), "provider": provider, "hosts": hosts}


def metric(rows, key, nested=None):
    values = [number((mapping(r.get(nested)) if nested else r).get(key)) for r in rows]
    seen = [v for v in values if v is not None]
    return {"observed_total": sum(seen) if seen else None, "observed_requests": len(seen),
            "missing_requests": len(rows) - len(seen),
            "coverage": len(seen) / len(rows) if rows else None}


def summary(rows):
    statuses = [r["status"] for r in rows if r["status"] is not None]
    failures = sum(s >= 400 for s in statuses)
    return {"calls": len(rows), "status_observed_calls": len(statuses),
            "failure_calls": failures if statuses else None,
            "failure_rate_observed": failures / len(statuses) if statuses else None,
            "status_coverage": len(statuses) / len(rows) if rows else None,
            "first_ts": min((r["ts"] for r in rows if r["ts"]), default=None),
            "last_ts": max((r["ts"] for r in rows if r["ts"]), default=None),
            "usage": {k: metric(rows, k, "usage") for k in USAGE},
            "timings_ms": {k: metric(rows, k) for k in TIMINGS},
            "cut": dict(Counter(r["cut"] or "not_observed" for r in rows)),
            "answers_source": dict(Counter(r["answers_source"] or "not_observed" for r in rows)),
            "telemetry_versions": dict(Counter(r["telemetry_version"] for r in rows)),
            "code_versions": dict(Counter(r["code_sha256"] or "unknown" for r in rows)),
            "revisions": dict(Counter(r["revision"] or "unknown" for r in rows)),
            "instances": dict(Counter(r["host"] for r in rows)),
            "statuses": dict(Counter(str(r["status"]) if r["status"] is not None else "unknown" for r in rows)),
            "routes": dict(Counter(r["route"] or "unknown" for r in rows))}


def cost_estimate(host, status, at, previous):
    """Freeze stopped upper bounds; unknown is NEVER a stopped observation."""
    old = mapping(previous)
    first_stop = old.get("first_stopped_observed_at")
    last_running = old.get("last_running_observed_at")
    # Ready fleet documented at 21:52; retain this evidence if first poll is STOPPED.
    baseline = "2026-11-19T21:52:00+09:00"
    if host != "zoro5" and last_running is None and seconds(at) >= seconds(baseline):
        last_running = baseline
    if status == "RUNNING":
        last_running = at
        first_stop = None
    elif status == "STOPPED" and not first_stop:
        first_stop = at
    earliest, latest = STARTS[host]
    # Last RUNNING is evidence of a snapshot, not a continuous billing interval.
    lower_end = last_running
    upper_end = first_stop or at
    low = max(0, seconds(lower_end) - seconds(latest)) if lower_end else 0
    high = max(low, seconds(upper_end) - seconds(earliest), 0)
    return {"basis": "runtime-estimate-assuming-continuous-allocation-since-start-bound",
            "actual_billing_interval": None, "actual_billed_usd": None,
            "nominal_start_bounds": {"earliest": earliest, "latest": latest},
            "last_running_observed_at": last_running, "first_stopped_observed_at": first_stop,
            "upper_estimate_through": upper_end,
            "unknown_interval_charged_as_confirmed_running": False,
            "runtime_seconds_estimate": {"lower": low, "upper": high},
            "gpu_usd_estimate": {"lower": round(low * RATES[host] / 3600, 6),
                                 "upper": round(high * RATES[host] / 3600, 6)},
            "disk_usd_estimate_assuming_storage_retained": {
                "lower": round(max(0, seconds(at) - seconds(latest)) * 0.6 / 86400, 6),
                "upper": round(max(0, seconds(at) - seconds(earliest)) * 0.6 / 86400, 6)},
            "actual_gpu_idle_seconds": None, "non_request_seconds_estimate": None,
            "non_request_estimate_unavailable_reason": "bounded logs do not prove complete nonoverlapping compute coverage"}


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def collect(observation, directory):
    at = timestamp(observation.get("at"))
    if at is None:
        raise ValueError("observation requires an aware ISO timestamp")
    prior = load_json(directory / "fleet-usage-latest.json", {})
    requests_path = directory / "fleet-usage-requests.jsonl"
    retained = {}
    if requests_path.exists():
        with requests_path.open() as stream:
            for line in stream:
                row = json.loads(line)
                retained[row["key"]] = row
    provider = mapping(observation.get("provider"))
    statuses = mapping(provider.get("statuses"))
    warnings: list[dict] = [{"condition": "unknownBilling", "detail": "actual balance and maximum quota unavailable; no credit threshold"}]
    running_count = number(provider.get("running_count"))
    if running_count is not None and running_count > MAX_RUNNING:
        warnings.append({"condition": "aboveApprovedRunning", "count": running_count, "allowed": MAX_RUNNING})
    hosts = {}
    new_rows = []
    prior_hosts = mapping(prior.get("hosts"))
    prior_scan_keys = mapping(prior.get("scan_request_keys"))
    scan_keys = {}
    for host in HOSTS:
        status = identifier(statuses.get(host)) if provider.get("available") is True else None
        remote = mapping(mapping(observation.get("hosts")).get(host))
        available = remote.get("available") is True
        previous_keys = set(prior_scan_keys.get(host, []))
        scan_keys[host] = [] if available else list(previous_keys)
        if not available and status != "STOPPED":
            warnings.append({"condition": "unreachable", "host": host})
        if seconds(at) >= seconds(DEADLINE) and status != "STOPPED":
            warnings.append({"condition": "afterDeadlineNotSTOPPED", "host": host, "provider_status": status})
        for index, raw in enumerate(remote.get("rows", []) if available else []):
            # Remote keys are stable file/inode/byte-offset hashes, not request IDs.
            key = mapping(raw).get("key")
            if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
                key = hashlib.sha256(f"fixture:{index}:{json.dumps(raw, sort_keys=True)}".encode()).hexdigest()
            row = metadata(raw, host, host + ":" + key)
            scan_keys[host].append(row["key"])
            if row["key"] not in retained and row["key"] not in previous_keys:
                new_rows.append(row)
            retained[row["key"]] = row
        logs = mapping(remote.get("logs"))
        health = mapping(remote.get("health"))
        models = remote.get("gpu_models")
        gpu_models = [m for m in models if isinstance(m, str) and re.fullmatch(r"[A-Za-z0-9 .()_+\-]{1,128}", m)] if isinstance(models, list) else None
        hosts[host] = {
            "provider_status": status, "provider_status_observed": status is not None,
            "ssh_observed": available, "observation_reason": identifier(remote.get("observation_reason")),
            "host_uptime_s": number(remote.get("uptime_s")) if available else None,
            "current_gpu_models": gpu_models or None,
            "gpu_model_observed": bool(gpu_models),
            "health_status": health.get("status") if health.get("status") in ("ok", "loading") else None,
            "service_uptime_s": number(health.get("uptime_s")),
            "revision": identifier(health.get("revision")),
            "on_disk_server_code_sha256": identifier(remote.get("code_sha256")),
            "loaded_code_sha256": identifier(health.get("code_sha256")),
            "telemetry_version": "usage-v1" if health.get("telemetry_version") == "usage-v1" else None,
            "logs": {"available": logs.get("available") is True,
                     "truncated": logs.get("truncated") if type(logs.get("truncated")) is bool else None,
                     "invalid_rows": number(logs.get("invalid_rows")),
                     "rows_scanned": number(logs.get("rows_scanned"))},
            "cost": cost_estimate(host, status, at, mapping(prior_hosts.get(host)).get("cost")),
        }
        if available and not hosts[host]["logs"]["available"]:
            warnings.append({"condition": "logsUnavailable", "host": host})
        if available and hosts[host]["health_status"] != "ok":
            warnings.append({"condition": "serviceNotReady", "host": host})
        if hosts[host]["logs"]["invalid_rows"]:
            warnings.append({"condition": "invalidLogRows", "host": host,
                             "count": hosts[host]["logs"]["invalid_rows"]})
    for condition, selected in (
        ("errorRequests", [r for r in new_rows if r["status"] is not None and r["status"] >= 400]),
        ("newCutoff", [r for r in new_rows if r["cut"]]),
        ("missingAttribution", [r for r in new_rows if r["attribution"]["owner"] == "unattributed"
                                 or any(r["attribution"][k] is None for k in ATTRIBUTION)]),
    ):
        if selected:
            warnings.append({"condition": condition, "new_calls": len(selected)})
    rows = sorted(retained.values(), key=lambda r: (r["ts"] or "", r["key"]))[-MAX_ROWS:]
    groups = {}
    for row in rows:
        attr = row["attribution"]
        fallback = None if all(attr[k] is not None for k in ATTRIBUTION[1:]) else row["request_id"] or row["key"]
        key = tuple(attr[k] for k in ATTRIBUTION) + (fallback,)
        groups.setdefault(key, []).append(row)
    aggregate = []
    for key, group in groups.items():
        aggregate.append({"attribution": dict(zip(ATTRIBUTION, key[:4])),
                          "request_id_fallback": key[4], **summary(group)})
    gpu = {bound: round(sum(h["cost"]["gpu_usd_estimate"][bound] for h in hosts.values()), 6)
           for bound in ("lower", "upper")}
    disk = {bound: round(sum(h["cost"]["disk_usd_estimate_assuming_storage_retained"][bound]
                            for h in hosts.values()), 6) for bound in ("lower", "upper")}
    result = {
        "schema_version": "fleet-usage-v1", "at": at, "deadline": DEADLINE,
        "provider_observed": provider.get("available") is True,
        "provider_running_count": running_count, "hosts": hosts,
        "billing": {"currency": "USD", "balance": None, "maximum_quota": None,
                    "actual_billing_interval": None, "actual_bill": None,
                    "gpu_hourly_rates": RATES, "historical_previous_usage_usd": 0.68,
                    "fleet_gpu_usd_estimate": gpu,
                    "gpu_plus_previous_usd_estimate": {k: round(v + 0.68, 6) for k, v in gpu.items()},
                    "gpu_disk_previous_usd_estimate": {
                        k: round(v + disk[k] + 0.68, 6) for k, v in gpu.items()},
                    "disk_estimate": {"gb_per_host": 125, "usd_per_host_day": 0.6,
                                      "fleet_usd_per_day": len(HOSTS) * 0.6, "persists_when_stopped": True,
                                      "cumulative_usd_estimate": disk, "billing_interval_unknown": True,
                                      "assumption": "125GB retained per host from creation bounds, including STOPPED"},
                    "zoro1_historical_disk_usd_per_hour_estimate": 0.03,
                    "zoro1_disk_current_allocation_confirmed": False,
                    "external_paid_inference_apis_in_stack": [],
                    "note": "runtime includes startup and idle; not token billing; estimates are not invoices"},
        "retention": {"request_limit": MAX_ROWS, "history_limit": 144,
                      "retained_calls": len(rows), "new_calls": len(new_rows),
                      "evicted_this_observation": max(0, len(retained) - MAX_ROWS),
                      "scope": "retained metadata only, not lifetime request totals",
                      "remote_scan_bytes_per_host": SCAN_BYTES, "remote_request_limit_per_host": REMOTE_ROWS},
        "totals": summary(rows), "by_attribution": aggregate,
        "by_instance": {host: summary([r for r in rows if r["host"] == host]) for host in HOSTS},
        "scan_request_keys": scan_keys, "warnings": warnings,
    }
    save(requests_path, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    save(directory / "fleet-usage-latest.json", json.dumps(result, indent=2) + "\n")
    history_path = directory / "fleet-usage-history.jsonl"
    history = deque(maxlen=144)
    if history_path.exists():
        with history_path.open() as stream:
            history.extend(stream)
    history.append(json.dumps({"schema_version": result["schema_version"], "at": at,
                               "hosts": hosts, "billing": result["billing"],
                               "totals": result["totals"], "warnings": warnings}) + "\n")
    save(history_path, "".join(history))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "logs")
    args = parser.parse_args(argv)
    if args.remote:
        print(json.dumps(remote_observation()))
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "fleet-usage.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        observation = json.loads(args.fixture.read_text()) if args.fixture else live_observation()
        result = collect(observation, args.output_dir)
        print(json.dumps({"at": result["at"], "output": str(args.output_dir / "fleet-usage-latest.json"),
                          "new_calls": result["retention"]["new_calls"], "warnings": result["warnings"]}), flush=True)


if __name__ == "__main__":
    main()
