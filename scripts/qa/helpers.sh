#!/usr/bin/env bash
# QA API test helpers: curl + jq, safe by default. Source this from a generated test script.
#
#   source "${QA_HELPERS:-/app/scripts/qa/helpers.sh}"
#   qa_case C1
#   api_call GET /items
#   check_http 200 "list items returns 200"
#   check_json '.items | length' 2 "two items"
#   qa_summary
#
# Environment:
#   QA_BASE_URL          target (required), e.g. http://127.0.0.1:8080
#   QA_OUT               output dir (default ./qa-out): results.jsonl + evidence/
#   QA_ALLOW_MUTATIONS   1 to allow methods other than GET/HEAD/OPTIONS (default 0: such calls are recorded as blocked)
#   QA_AUTH_HEADER       full header, e.g. "Authorization: Bearer ..."  (never written to evidence)
#   QA_USERNAME/QA_PASSWORD  basic auth alternative
#   QA_TIMEOUT           per-request seconds (default 30)
#   QA_INSECURE_TLS      1 to accept self-signed certificates (curl -k)
#
# There is deliberately no `set -e`: every check runs and qa_summary always prints (it always exits 0; the
# report is the gate). Safety rules enforced here, because test scripts are written from untrusted PR text:
#   * a request is sent only to QA_BASE_URL (absolute URLs must start with it) and only with a vetted method
#   * extra curl arguments are an allowlist (-H, -d, --data*, --json, -F, -G, -I, -i, -A, -b, -m, --compressed); anything else is refused
#   * no response, a curl error, or a 401/403 without credentials is `blocked` (infrastructure), never a product failure
# Records are JSON Lines in $QA_OUT/results.jsonl; evidence files hold the first 4000 bytes of each response.

QA_OUT="${QA_OUT:-./qa-out}"
QA_RESULTS="${QA_RESULTS:-$QA_OUT/results.jsonl}"
QA_TIMEOUT="${QA_TIMEOUT:-30}"
QA_ALLOW_MUTATIONS="${QA_ALLOW_MUTATIONS:-0}"
mkdir -p "$QA_OUT/evidence"
: >> "$QA_RESULTS"

QA_CASE="C0"; _QA_N=0; _QA_LAST_OK=1
_QA_COUNT_pass=0; _QA_COUNT_fail=0; _QA_COUNT_blocked=0; _QA_COUNT_not_run=0
HTTP_CODE=0; HTTP_BODY=""; _QA_LAST_CMD=""; _QA_LAST_EVIDENCE=""
_QA_TOOLS_MISSING=""
for _qa_tool in curl jq; do command -v "$_qa_tool" >/dev/null 2>&1 || _QA_TOOLS_MISSING="$_QA_TOOLS_MISSING $_qa_tool"; done
unset _qa_tool

# Case ids become file names: keep them to a safe alphabet.
qa_case() { QA_CASE="${1//[^A-Za-z0-9_.-]/_}"; _QA_N=0; _QA_LAST_OK=1; }

# _qa_record OUTCOME MESSAGE [infra=0|1]
_qa_record() {
  local outcome="$1" msg="${2:0:500}" infra="${3:-0}" observed
  observed="HTTP ${HTTP_CODE}: ${HTTP_BODY:0:300}"
  if ! jq -cn --arg case "$QA_CASE" --arg outcome "$outcome" --arg message "$msg" --arg command "$_QA_LAST_CMD" \
        --arg observed "$observed" --arg evidence "$_QA_LAST_EVIDENCE" --argjson infra "$([ "$infra" = 1 ] && echo true || echo false)" \
        '{case:$case,outcome:$outcome,message:$message,command:$command,observed:$observed,evidence:$evidence,infra:$infra}' >> "$QA_RESULTS" 2>/dev/null; then
    # jq missing or the record could not be encoded: keep a minimal, always-valid line so the failure is not lost.
    printf '{"case":"%s","outcome":"%s","message":"record could not be encoded","command":"","observed":"","evidence":"","infra":true}\n' \
      "$QA_CASE" "$([ "$outcome" = pass ] && echo inconclusive || echo "$outcome")" >> "$QA_RESULTS"
  fi
  case "$outcome" in
    pass) _QA_COUNT_pass=$((_QA_COUNT_pass + 1)); printf 'PASS  [%s] %s\n' "$QA_CASE" "$msg" ;;
    fail) _QA_COUNT_fail=$((_QA_COUNT_fail + 1)); printf 'FAIL  [%s] %s\n' "$QA_CASE" "$msg" ;;
    blocked) _QA_COUNT_blocked=$((_QA_COUNT_blocked + 1)); printf 'BLOCK [%s] %s\n' "$QA_CASE" "$msg" ;;
    *) _QA_COUNT_not_run=$((_QA_COUNT_not_run + 1)); printf 'SKIP  [%s] %s\n' "$QA_CASE" "$msg" ;;
  esac
}

qa_pass() { _qa_record pass "$1"; }
qa_fail() { _qa_record fail "$1"; }
qa_skip() { _qa_record not-run "$1"; }
qa_blocked() { _qa_record blocked "$1"; }
qa_infra() { _qa_record blocked "$1" 1; }

# A request may only go to QA_BASE_URL. Compare by prefix, never by parsed host: URL parsers disagree about
# `http://evil#@good` and friends, and the port and scheme must match too.
_qa_url_allowed() {
  local url="$1" base="${QA_BASE_URL%/}"
  case "$url" in *[[:space:][:cntrl:]]*) return 1 ;; esac
  case "$url" in "$base"|"$base"/*|"$base"\?*|"$base"\#*) return 0 ;; *) return 1 ;; esac
}

# Extra curl arguments are an allowlist, not a denylist: clustered short options (-sL, -sXPOST), extra URLs and
# anything that follows redirects, changes the method, uses a proxy/config, or reads/writes local files are refused.
_QA_VET_ERROR=""
_qa_vet_curl_args() {
  local arg expect_value=0 value_opt=""
  for arg in "$@"; do
    if [ "$expect_value" = 1 ]; then
      expect_value=0
      case "$arg" in
        @*) _QA_VET_ERROR="reading a local file into a request ($value_opt $arg) is not allowed in api_call"; return 1 ;;
      esac
      case "$value_opt" in -F|--form) case "$arg" in *=@*|*=\<*) _QA_VET_ERROR="reading a local file into a request ($value_opt $arg) is not allowed in api_call"; return 1 ;; esac ;; esac
      continue
    fi
    case "$arg" in
      -H|--header|-d|--data|--data-raw|--data-binary|--data-urlencode|--json|-F|--form|-A|--user-agent|-b|--cookie|-m|--max-time)
        expect_value=1; value_opt="$arg" ;;
      -G|--get|-I|--head|-i|--include|--compressed) ;;
      -*) _QA_VET_ERROR="curl option $arg is not allowed in api_call"; return 1 ;;
      *) _QA_VET_ERROR="unexpected argument '$arg': api_call takes METHOD, PATH and curl options only"; return 1 ;;
    esac
  done
  if [ "$expect_value" = 1 ]; then _QA_VET_ERROR="curl option $value_opt needs a value"; return 1; fi
  return 0
}

# api_call METHOD PATH_OR_URL [extra curl args...]  -> sets HTTP_CODE, HTTP_BODY
api_call() {
  local method url target body_file err_file n code rc arg
  method="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"; target="$2"; shift 2
  _QA_N=$((_QA_N + 1)); n="$_QA_N"
  HTTP_CODE=0; HTTP_BODY=""; _QA_LAST_OK=0; _QA_LAST_EVIDENCE=""
  _QA_LAST_CMD="$method $target"
  if [ -n "$_QA_TOOLS_MISSING" ]; then qa_infra "required tool(s) missing:$_QA_TOOLS_MISSING"; return 1; fi
  if [ -z "${QA_BASE_URL:-}" ]; then qa_blocked "QA_BASE_URL is not set"; return 1; fi
  case "$target" in http://*|https://*) url="$target" ;; *) url="${QA_BASE_URL%/}/${target#/}" ;; esac
  _QA_LAST_CMD="curl -X $method $url"
  if ! _qa_url_allowed "$url"; then
    qa_blocked "refused request outside the declared target ${QA_BASE_URL}"; return 1
  fi
  case "$method" in
    GET|HEAD|OPTIONS) ;;
    *) if [ "$QA_ALLOW_MUTATIONS" != "1" ]; then qa_blocked "mutating call $method blocked (set --allow-mutations / QA_ALLOW_MUTATIONS=1)"; return 1; fi ;;
  esac
  if ! _qa_vet_curl_args "$@"; then qa_blocked "$_QA_VET_ERROR"; return 1; fi
  body_file="$(mktemp)"; err_file="$(mktemp)"
  local auth=()
  [ "${QA_INSECURE_TLS:-0}" = "1" ] && auth+=(-k)
  if [ -n "${QA_AUTH_HEADER:-}" ]; then auth+=(-H "$QA_AUTH_HEADER")
  elif [ -n "${QA_USERNAME:-}" ]; then auth+=(-u "$QA_USERNAME:${QA_PASSWORD:-}"); fi
  # ${auth[@]+...}: an empty array is an unbound variable under `set -u` on bash 3.2 (macOS).
  # ulimit -f caps what a response can write to disk (20 MB); --proto keeps curl to http(s)
  code="$( ulimit -f 40960 2>/dev/null; curl -sS -o "$body_file" -w '%{http_code}' -X "$method" --proto '=http,https' --max-filesize 20971520 --max-time "$QA_TIMEOUT" ${auth[@]+"${auth[@]}"} "$@" "$url" 2>"$err_file")"
  rc=$?
  HTTP_CODE="${code:-0}"
  HTTP_BODY="$(head -c 1048576 "$body_file" | tr -d '\0')"
  _QA_LAST_EVIDENCE="$QA_OUT/evidence/${QA_CASE}-${n}.txt"
  { printf '%s\nstatus: %s\n---\n' "$_QA_LAST_CMD" "$HTTP_CODE"; printf '%s' "${HTTP_BODY:0:4000}"; printf '\n---\ncurl exit: %s\n' "$rc"; cat "$err_file"; } > "$_QA_LAST_EVIDENCE"
  local curl_err; curl_err="$(head -c 200 "$err_file" | tr '\n' ' ')"
  rm -f "$body_file" "$err_file"
  if [ "$rc" -ne 0 ]; then
    # A stalled or reset transfer can leave a 200 header and half a body: never treat that as a response.
    qa_infra "curl failed (exit $rc) for $url: $curl_err"; return 1
  fi
  _QA_LAST_OK=1
  return 0
}

# check_http EXPECTED_CODE [message]
check_http() {
  [ "$_QA_LAST_OK" = 1 ] || return 0   # the failed/blocked call was already recorded
  if [ "$HTTP_CODE" = "$1" ]; then qa_pass "${2:-HTTP $1}"; return 0; fi
  if { [ "$HTTP_CODE" = 401 ] || [ "$HTTP_CODE" = 403 ]; } && [ -z "${QA_AUTH_HEADER:-}${QA_USERNAME:-}" ]; then
    qa_infra "${2:-HTTP $1}: target answered $HTTP_CODE and no credentials were provided (QA_AUTH_HEADER / QA_USERNAME); criteria cannot be verified"; return 0
  fi
  qa_fail "${2:-HTTP $1}: expected $1, got $HTTP_CODE"
}

# check_eq EXPECTED ACTUAL message
check_eq() { if [ "$1" = "$2" ]; then qa_pass "$3"; else qa_fail "$3: expected '${1:0:200}', got '${2:0:200}'"; fi; }

# check_json JQ_EXPR EXPECTED message   (compares jq -r output of the last response body)
check_json() {
  [ "$_QA_LAST_OK" = 1 ] || return 0
  local actual
  if ! actual="$(printf '%s' "$HTTP_BODY" | jq -r "$1" 2>/dev/null)"; then qa_fail "$3: response body is not valid JSON or the jq filter is invalid"; return 0; fi
  check_eq "$2" "$actual" "$3"
}

# check_contains NEEDLE message  (substring of the last response body)
check_contains() {
  [ "$_QA_LAST_OK" = 1 ] || return 0
  case "$HTTP_BODY" in *"$1"*) qa_pass "$2" ;; *) qa_fail "$2: body does not contain '${1:0:200}'" ;; esac
}

qa_summary() {
  printf 'QA_RESULTS_JSON:{"pass":%s,"fail":%s,"blocked":%s,"not_run":%s}\n' \
    "$_QA_COUNT_pass" "$_QA_COUNT_fail" "$_QA_COUNT_blocked" "$_QA_COUNT_not_run"
  if [ $((_QA_COUNT_pass + _QA_COUNT_fail)) -eq 0 ]; then
    printf 'QA_WARNING: no check executed (all blocked or skipped); this run proves nothing\n'
  fi
}
