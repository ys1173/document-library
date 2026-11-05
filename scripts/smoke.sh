#!/bin/bash
# End to end: real product images (non-root, read-only filesystem), real vision model, real database roles.
set -u  # No pipefail: `cmd | grep -q` closes the pipe early, and that would count as a failure.
cd "$(dirname "$0")/.."
export DOCLIB_SHARE="$PWD/deploy/sample-share"
dc="docker compose -p doclib-smoke -f deploy/docker-compose.yml -f deploy/compose.postgres.yml -f deploy/compose.vision.yml -f deploy/test/compose.smoke.yml"
# SMOKE_EMBED=real runs the real embedding model and checks retrieval quality. The default is a fast fake.
if [ "${SMOKE_EMBED:-fake}" = "real" ]; then dc="$dc -f deploy/compose.embed.yml"; else dc="$dc -f deploy/test/compose.fake-embed.yml"; fi
pass=0; fail=0
check() { if [ "$2" = "0" ]; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
api() { $dc exec -T webhook python -c "
import json,sys,urllib.request as u
path,body=sys.argv[1],sys.argv[2]
r=u.Request('http://api:8080'+path,data=body.encode() if body else None,headers={'Content-Type':'application/json'})
print(u.urlopen(r,timeout=120).read().decode())" "$1" "${2:-}" </dev/null; }
hooks() { $dc exec -T webhook python -c "import urllib.request as u; print(u.urlopen('http://localhost:9000/messages').read().decode())" </dev/null; }
wait_for() { # description, seconds, command
  local end=$((SECONDS+$2)); while [ $SECONDS -lt $end ]; do if eval "$3" >/dev/null 2>&1; then check "$1" 0; return 0; fi; sleep 3; done; check "$1" 1; return 1; }
cleanup() { $dc logs --no-color > /tmp/doclib-smoke.log 2>&1 || true; $dc down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

scripts/make-secrets.sh >/dev/null
docker compose -f deploy/test/compose.yml build -q tests
touch deploy/secrets/vision_api_key   # Only used in remote mode, but compose needs the file to exist.
rm -rf "$DOCLIB_SHARE"; mkdir -p "$DOCLIB_SHARE"; chmod 755 "$DOCLIB_SHARE"
docker run --rm -v "$PWD/tests:/src/tests:ro" -v "$DOCLIB_SHARE:/out" -w /src doclib-test-tests python -c "
from pathlib import Path
from tests.fakes.fixtures import make_corpus, make_scanned_pdf
root=Path('/out'); make_corpus(root); make_scanned_pdf(root/'scan.pdf')"
find "$DOCLIB_SHARE" -type f -exec chmod a+r {} +; find "$DOCLIB_SHARE" -type d -exec chmod a+rx {} +
before=$(cd "$DOCLIB_SHARE" && find . -type f -exec shasum {} + | sort | shasum)

$dc build -q scanner vision 2>&1 | tail -2
if [ "${SMOKE_EMBED:-fake}" = "real" ]; then $dc build -q embed 2>&1 | tail -1; fi
# SMOKE_VISION_URL points at a vision server elsewhere (the remote GPU case) instead of the local container.
if [ -n "${SMOKE_VISION_URL:-}" ]; then scale="--scale vision=0"; echo "vision: remote at $SMOKE_VISION_URL"; else scale=""; fi
$dc up -d $scale 2>&1 | tail -1
wait_for "API is up, running as the read-only role" 60 "api /healthz"
admin_call() { $dc exec -T webhook python -c "
import sys,urllib.request as u
print(u.urlopen('http://admin:8081'+sys.argv[1],timeout=30).read().decode())" "$1" </dev/null; }
wait_for "admin API is up and lists rclone backends" 60 "admin_call /v1/connector-types | grep -q '\"s3\"'"

wait_for "initial index: 8 files indexed" 90 "api /v1/list_sources | grep -q '\"indexed\": *8\|\"indexed\":8'"
api /v1/search_docs '{"query":"quokka alarms"}' | grep -q '"degraded":\[\]'; check "search: both paths healthy" $?
api /v1/search_docs '{"query":"quokka alarms"}' | grep -q 'notes.md'; check "search: finds the right file" $?
uri=$(api /v1/search_docs '{"query":"restarting SMF pods"}' | python3 -c "import json,sys; print(next(r['citation']['uri'] for r in json.load(sys.stdin)['results'] if r['citation']['path'].endswith('Guide.pdf')))")
api /v1/open_doc "{\"uri\":\"$uri\"}" | grep -q 'Restart SMF pods'; check "citation opens the live file" $?
api /v1/search_docs '{"query":"AMF pods maintenance"}' | grep -q 'scan.pdf'; check "scanned PDF searchable straight away (OCR)" $?
api /v1/index_status | grep -q '"reachable":true'; check "index_status reports the vision server reachable" $?
hooks | grep -q '"changes"'; [ $? -ne 0 ]; check "first index sent no notifications" $?

if [ "${SMOKE_EMBED:-fake}" = "real" ]; then
  # Keyword search can't match these (no shared words, or Japanese against English), so hits prove real semantic search.
  top() { api /v1/search_docs "{\"query\":\"$1\",\"top_k\":3}" | python3 -c "
import json,sys
r=json.load(sys.stdin); print(r['degraded'], [(x['citation']['path'], x['score']) for x in r['results']])"; }
  expect() { out=$(top "$1"); echo "      [$1] -> $out"; echo "$out" | head -c 90 | grep -q "$2"; check "semantic search: $3" $?; }
  expect "bringing a network function down for a software update safely" "Guide.pdf" "paraphrase finds the guide"
  expect "SMFポッドを再起動するにはどうすればよいですか" "Guide.pdf\|ja.txt" "Japanese question finds SMF restart text"
  expect "拠点のサブネット割り当て表" "IP Plan.xlsx" "Japanese question finds an English spreadsheet"
fi

wait_for "real vision model upgraded the scanned page" 300 "api /v1/search_docs '{\"query\":\"AMF pods maintenance\"}' | grep -q '\"extraction_path\":\"vlm\"'"

printf '# Added\n\nThe okapi escalation procedure.\n' > "$DOCLIB_SHARE/added.md"; chmod a+r "$DOCLIB_SHARE/added.md"
mv "$DOCLIB_SHARE/router.cfg" "$DOCLIB_SHARE/router2.cfg"
rm "$DOCLIB_SHARE/Design.pptx"
wait_for "drive change: new file searchable automatically" 60 "api /v1/search_docs '{\"query\":\"okapi escalation\"}' | grep -q 'added.md'"
wait_for "webhook digest received for the changes" 30 "hooks | grep -q 'added.md'"
hooks | python3 -c "
import json,sys
ms=json.load(sys.stdin); s={}
for m in ms:
    for k,v in m['summary'].items(): s[k]=s.get(k,0)+v
print('      webhook totals:',s,'| example text:',ms[0]['text'])
sys.exit(0 if s['new']==1 and s['moved']==1 and s['deleted']==1 else 1)"; check "webhook reports new, moved and deleted" $?

printf '# Added\n\nEdited: the tapir procedure.\n' > "$DOCLIB_SHARE/added.md"
api /v1/open_doc '{"source_id":"share","path":"added.md"}' | grep -q '"stale_index":true\|tapir'; check "open_doc reads the live edit" $?

denied=$($dc exec -T postgres sh -c 'PGPASSWORD="$(cat /run/secrets/doclib_api_password)" psql -h localhost -U doclib_api -d doclib -c "delete from doclib.chunks"' </dev/null 2>&1)
echo "$denied" | grep -q 'permission denied'; check "API database role cannot write" $?

logs=$($dc logs --no-color scanner worker vlm-worker api 2>/dev/null)
echo "$logs" | grep -q 'quokka alarms\|okapi escalation'; [ $? -ne 0 ]; check "no document or query text in logs" $?
echo "      log lines: $(echo "$logs" | grep -c '"event"')  events: $(echo "$logs" | grep -o '"event": *"[a-z_.]*"' | sort -u | wc -l | tr -d ' ') distinct"

for s in worker api; do c=$($dc exec -T $s sh -c 'id -u; touch /app-test 2>&1 | grep -c "Read-only"; find /tmp -type f | wc -l' </dev/null); echo "      $s: uid/readonly/tmpfiles = $(echo $c)"; done

echo; echo "== licence gate"
docker run --rm -i --entrypoint python doclib:0.1.0 - < scripts/licence_report.py > /tmp/doclib-licences.csv; check "licence gate: product image" $?
docker run --rm -i --entrypoint python doclib-vision:cpu - < scripts/licence_report.py > /tmp/doclib-vision-licences.csv; rc=$?; check "licence gate: vision image" $rc
grep -hE ',(review|BLOCKED|UNKNOWN)$' /tmp/doclib-licences.csv /tmp/doclib-vision-licences.csv | sort -u | head -25
if [ "${SMOKE_EMBED:-fake}" = "real" ]; then docker run --rm -i --entrypoint python doclib-embed:cpu - < scripts/licence_report.py > /tmp/doclib-embed-licences.csv; check "licence gate: embedding image" $?; fi
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E '^doclib(-vision)?:'
echo; echo "SMOKE: $pass passed, $fail failed"
[ "$fail" = "0" ]
