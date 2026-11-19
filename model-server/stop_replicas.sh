#!/usr/bin/env bash
# Provider-level backup for authorized replicas and replacement; zoro2 guard is untouched.
set -euo pipefail
cd "$(dirname "$0")"
DEADLINE=$(date -j -f '%Y-%m-%d %H:%M:%S %z' '2026-11-20 14:00:00 +0900' '+%s')
[ "$(date '+%s')" -ge "$DEADLINE" ] || exit 0
[ ! -f logs/replicas-stopped.confirmed ] || exit 0
for instance in zoro4 zoro5; do
  /opt/homebrew/bin/brev stop "$instance" </dev/null || true
done
/opt/homebrew/bin/brev ls --json | /opt/homebrew/bin/python3 -c '
import json,pathlib,sys,datetime
wanted={"zoro3","zoro4","zoro5"}
rows=[{"name":w["name"],"status":w.get("status")} for w in json.load(sys.stdin).get("workspaces",[]) if w.get("name") in wanted]
report={"at":datetime.datetime.now().astimezone().isoformat(),"replicas":rows}
pathlib.Path("logs/replica-stop-status.json").write_text(json.dumps(report,indent=2))
with pathlib.Path("../docs/zoro-requests.md").open("a") as f:
    f.write("\n- replica stop verification: "+json.dumps(report)+"\n")
if {r["name"] for r in rows}==wanted and all(r["status"]=="STOPPED" for r in rows):
    pathlib.Path("logs/replicas-stopped.confirmed").write_text(report["at"])
print(json.dumps(report))
'
