#!/usr/bin/env bash

################################################################################
# Integration - Chaos / Self-Healing Validation
#
# Scenarios:
#   H1. Delete one inference pod and verify serving continuity plus replacement
#       recovery under live search traffic.
#   H2. Delete one primary event-consumer pod and verify replacement recovery,
#       continued click ingestion, and post-recovery personalization catch-up.
#
# Notes:
# - This is a production validation flow, not a full chaos platform.
# - Automated checks focus on destructive action + bounded health/recovery checks.
# - Consumer lag spike / catch-up remains a documented Prometheus verification
#   step because the repo does not ship a ready-to-query Kubernetes Prometheus
#   service or a broker-side lag inspection helper for the test environment.
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

RESULT_FILE="$TEST_OUTPUT_DIR/chaos_results_${TEST_TIMESTAMP}.md"

NAMESPACE="${NAMESPACE:-scalestyle}"
CHAOS_SCENARIO="${CHAOS_SCENARIO:-all}"   # all | inference | consumer

INFERENCE_DEPLOYMENT="${INFERENCE_DEPLOYMENT:-inference}"
INFERENCE_LABEL="${INFERENCE_LABEL:-component=inference}"
INFERENCE_LOAD_DURATION_SEC="${INFERENCE_LOAD_DURATION_SEC:-75}"
INFERENCE_DELETE_DELAY_SEC="${INFERENCE_DELETE_DELAY_SEC:-15}"
INFERENCE_LOAD_CONCURRENCY="${INFERENCE_LOAD_CONCURRENCY:-8}"
INFERENCE_RECOVERY_TIMEOUT_SEC="${INFERENCE_RECOVERY_TIMEOUT_SEC:-180}"
INFERENCE_MAX_ERROR_RATE="${INFERENCE_MAX_ERROR_RATE:-0.10}"

CONSUMER_DEPLOYMENT="${CONSUMER_DEPLOYMENT:-event-consumer-primary}"
CONSUMER_LABEL="${CONSUMER_LABEL:-component=event-consumer,consumer-mode=primary}"
CONSUMER_LOAD_DURATION_SEC="${CONSUMER_LOAD_DURATION_SEC:-90}"
CONSUMER_DELETE_DELAY_SEC="${CONSUMER_DELETE_DELAY_SEC:-15}"
CONSUMER_LOAD_CONCURRENCY="${CONSUMER_LOAD_CONCURRENCY:-4}"
CONSUMER_RECOVERY_TIMEOUT_SEC="${CONSUMER_RECOVERY_TIMEOUT_SEC:-180}"
CONSUMER_MAX_ACK_ERROR_RATE="${CONSUMER_MAX_ACK_ERROR_RATE:-0.05}"
CONSUMER_PROBE_TIMEOUT_SEC="${CONSUMER_PROBE_TIMEOUT_SEC:-90}"

require_tooling() {
    local missing=false
    for tool in kubectl curl jq python3; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            log_error "Required tool not found: $tool"
            missing=true
        fi
    done

    if [[ "$missing" == "true" ]]; then
        exit 1
    fi
}

record_markdown_header() {
    {
        echo "# Kubernetes Chaos / Self-Healing Validation"
        echo ""
        echo "**Test Run**: $(date -Iseconds)"
        echo "**Namespace**: $NAMESPACE"
        echo "**Scenario Selection**: $CHAOS_SCENARIO"
        echo "**Gateway URL**: $GATEWAY_URL"
        echo ""
        echo "---"
        echo ""
    } > "$RESULT_FILE"
}

print_inference_expected_signals() {
    log_info "Expected operator signals during inference pod deletion:"
    log_info "  - Grafana resilience dashboard: Degradation Rate (%) may spike"
    log_info "  - Grafana overview dashboard: Fallback Rate (Degraded Service) may rise"
    log_info "  - Ray Call Status should show degraded/fallback traffic replacing some ray hits"
    log_info "  - Inference deployment available replicas should return to the desired count"
}

print_consumer_expected_signals() {
    log_info "Expected operator signals during event-consumer pod deletion:"
    log_info "  - Prometheus: sum(kafka_consumer_lag{topic=\"scalestyle.clicks\"}) should rise then fall"
    log_info "  - Prometheus: sum(rate(events_processed_total{result=\"applied\"}[1m])) should dip then recover"
    log_info "  - Prometheus: consumer_health for the replacement pod should return to 1"
    log_info "  - Alert rules that may become relevant: KafkaConsumerGroupLagHigh, KafkaConsumerGroupLagCritical"
}

start_inference_load() {
    local output_file="$1"

    python3 - "$output_file" "$GATEWAY_URL" "$INFERENCE_LOAD_DURATION_SEC" "$INFERENCE_LOAD_CONCURRENCY" <<'PYTHON_SCRIPT' &
import json
import sys
import threading
import time
from urllib import error, parse, request

output_file, gateway_url, duration_sec, concurrency = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
queries = ["dress", "jacket", "shirt", "jeans", "shoes"]
deadline = time.time() + duration_sec
results = []
lock = threading.Lock()
start = time.time()

def one_request(worker_id, seq):
    params = parse.urlencode({"query": queries[(worker_id + seq) % len(queries)], "k": 10})
    url = f"{gateway_url}/api/recommendation/search?{params}"
    req = request.Request(url, method="GET")
    req_start = time.time()
    try:
        with request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            data = payload.get("data") or []
            first = data[0] if data else {}
            return {
                "timestamp": time.time() - start,
                "http_code": resp.status,
                "status": "success",
                "latency_ms": (time.time() - req_start) * 1000.0,
                "degraded": bool(first.get("degraded", False)),
                "source": str(first.get("source", "unknown")),
            }
    except error.HTTPError as exc:
        return {
            "timestamp": time.time() - start,
            "http_code": exc.code,
            "status": "http_error",
            "latency_ms": (time.time() - req_start) * 1000.0,
            "degraded": False,
            "source": "http_error",
        }
    except Exception:
        return {
            "timestamp": time.time() - start,
            "http_code": 0,
            "status": "exception",
            "latency_ms": (time.time() - req_start) * 1000.0,
            "degraded": False,
            "source": "exception",
        }

def worker(worker_id):
    local_results = []
    seq = 0
    while time.time() < deadline:
        local_results.append(one_request(worker_id, seq))
        seq += 1
    with lock:
        results.extend(local_results)

threads = [threading.Thread(target=worker, args=(idx,), daemon=True) for idx in range(concurrency)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(results, handle)
PYTHON_SCRIPT

    echo $!
}

analyze_inference_load() {
    local output_file="$1"
    local report_file="$2"
    local delete_delay_sec="$3"
    local recovery_seconds="$4"
    local recovered_flag="$5"

    python3 - "$output_file" "$report_file" "$delete_delay_sec" "$INFERENCE_MAX_ERROR_RATE" "$recovery_seconds" "$recovered_flag" <<'PYTHON_ANALYSIS'
import json
import sys

results_file, report_file, delete_delay_sec, max_error_rate, recovery_seconds, recovered_flag = sys.argv[1:7]
delete_delay_sec = float(delete_delay_sec)
max_error_rate = float(max_error_rate)
recovery_seconds = float(recovery_seconds)
recovered = recovered_flag == "true"

with open(results_file, "r", encoding="utf-8") as handle:
    results = json.load(handle)

pre = [r for r in results if r.get("timestamp", 0) < delete_delay_sec]
post = [r for r in results if r.get("timestamp", 0) >= delete_delay_sec]

def summarize(rows):
    total = len(rows)
    successes = [r for r in rows if r.get("http_code") == 200]
    errors = total - len(successes)
    degraded = [r for r in successes if r.get("degraded") or r.get("source") != "ray"]
    ray_success = [r for r in successes if not r.get("degraded") and r.get("source") == "ray"]
    latencies = sorted(r.get("latency_ms", 0.0) for r in successes)
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0
    return {
        "total": total,
        "successes": len(successes),
        "errors": errors,
        "error_rate": (errors / total) if total else 1.0,
        "degraded": len(degraded),
        "degraded_ratio": (len(degraded) / len(successes)) if successes else 0.0,
        "ray_successes": len(ray_success),
        "p99_ms": p99,
    }

pre_summary = summarize(pre)
post_summary = summarize(post)

pass_serving = post_summary["successes"] > 0 and post_summary["error_rate"] <= max_error_rate
pass_recovery = recovered
pass_all = pass_serving and pass_recovery

with open(report_file, "a", encoding="utf-8") as handle:
    handle.write("## H1. Inference Pod Deletion Under Search Traffic\n\n")
    handle.write("**Objective**: Delete one inference pod, keep the gateway serving, and verify the deployment self-heals back to the desired replica count.\n\n")
    handle.write("### Expected Operator Signals\n\n")
    handle.write("- Grafana: `ScaleStyle - Resilience` degradation rate and Ray failure traffic may spike briefly.\n")
    handle.write("- Grafana: `ScaleStyle - Recommendation Service Overview` fallback rate may rise.\n")
    handle.write("- Kubernetes: inference deployment available replicas should return to the desired count.\n\n")
    handle.write("### Automated Results\n\n")
    handle.write("| Window | Total | Successes | Errors | Error Rate | Degraded Successes | Degraded Ratio | Ray Successes | P99 |\n")
    handle.write("|--------|-------|-----------|--------|------------|--------------------|----------------|---------------|-----|\n")
    handle.write(f"| Pre-delete | {pre_summary['total']} | {pre_summary['successes']} | {pre_summary['errors']} | {pre_summary['error_rate']:.4f} | {pre_summary['degraded']} | {pre_summary['degraded_ratio']:.4f} | {pre_summary['ray_successes']} | {pre_summary['p99_ms']:.0f}ms |\n")
    handle.write(f"| Post-delete | {post_summary['total']} | {post_summary['successes']} | {post_summary['errors']} | {post_summary['error_rate']:.4f} | {post_summary['degraded']} | {post_summary['degraded_ratio']:.4f} | {post_summary['ray_successes']} | {post_summary['p99_ms']:.0f}ms |\n\n")
    handle.write("### Recovery Detection\n\n")
    handle.write(f"- Deployment recovered to desired replicas: {'yes' if recovered else 'no'}\n")
    handle.write(f"- Recovery time: {recovery_seconds:.1f}s\n")
    handle.write(f"- Post-delete error-rate threshold: <= {max_error_rate:.2f}\n")
    if post_summary['degraded'] == 0:
        handle.write("- Observed degraded responses: none in sampled traffic. This can happen if remaining capacity absorbs the fault; use Grafana to confirm whether fallback spiked at lower resolution.\n")
    else:
        handle.write(f"- Observed degraded responses after deletion: {post_summary['degraded']}\n")
    handle.write("\n")
    handle.write(f"**Status**: {'✅ PASS' if pass_all else '❌ FAIL'}\n\n")

print(json.dumps({
    "pass": pass_all,
    "post_error_rate": post_summary["error_rate"],
    "post_degraded": post_summary["degraded"],
    "recovered": recovered,
    "recovery_seconds": recovery_seconds,
}))
sys.exit(0 if pass_all else 1)
PYTHON_ANALYSIS
}

start_click_load() {
    local output_file="$1"
    local user_prefix="$2"

    python3 - "$output_file" "$GATEWAY_URL" "$CONSUMER_LOAD_DURATION_SEC" "$CONSUMER_LOAD_CONCURRENCY" "$user_prefix" <<'PYTHON_SCRIPT' &
import json
import sys
import threading
import time
from urllib import error, request

output_file, gateway_url, duration_sec, concurrency, user_prefix = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
items = ["chaos-item-a", "chaos-item-b", "chaos-item-c"]
deadline = time.time() + duration_sec
results = []
lock = threading.Lock()
start = time.time()

def send_click(worker_id, seq):
    payload = json.dumps({
        "user_id": f"{user_prefix}-{worker_id}",
        "item_id": items[seq % len(items)],
        "session_id": f"sess-{user_prefix}-{worker_id}",
        "source": "search",
        "query": "dress",
        "position": seq % 10,
        "device": "chaos-test",
    }).encode("utf-8")
    req = request.Request(
        f"{gateway_url}/api/events/click",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    req_start = time.time()
    try:
        with request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return {
                "timestamp": time.time() - start,
                "http_code": resp.status,
                "status": "success",
                "latency_ms": (time.time() - req_start) * 1000.0,
                "event_status": str(((body.get("data") or {}).get("status") or "unknown")),
            }
    except error.HTTPError as exc:
        return {
            "timestamp": time.time() - start,
            "http_code": exc.code,
            "status": "http_error",
            "latency_ms": (time.time() - req_start) * 1000.0,
            "event_status": "http_error",
        }
    except Exception:
        return {
            "timestamp": time.time() - start,
            "http_code": 0,
            "status": "exception",
            "latency_ms": (time.time() - req_start) * 1000.0,
            "event_status": "exception",
        }

def worker(worker_id):
    local_results = []
    seq = 0
    while time.time() < deadline:
        local_results.append(send_click(worker_id, seq))
        seq += 1
    with lock:
        results.extend(local_results)

threads = [threading.Thread(target=worker, args=(idx,), daemon=True) for idx in range(concurrency)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(results, handle)
PYTHON_SCRIPT

    echo $!
}

analyze_click_load() {
    local output_file="$1"

    python3 - "$output_file" <<'PYTHON_ANALYSIS'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    results = json.load(handle)

total = len(results)
successes = [r for r in results if r.get("http_code") == 200]
errors = total - len(successes)
latencies = sorted(r.get("latency_ms", 0.0) for r in successes)
p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0.0
ack_success = [r for r in successes if r.get("event_status") == "acknowledged_by_broker"]

print(json.dumps({
    "total": total,
    "successes": len(successes),
    "errors": errors,
    "error_rate": (errors / total) if total else 1.0,
    "ack_successes": len(ack_success),
    "p99_ms": p99,
}))
PYTHON_ANALYSIS
}

run_consumer_recovery_probe() {
    local summary_file="$1"

    python3 - "$summary_file" "$GATEWAY_URL" "$CONSUMER_PROBE_TIMEOUT_SEC" <<'PYTHON_PROBE'
import json
import sys
import time
import uuid
from urllib import error, parse, request

summary_file, gateway_url, timeout_sec = sys.argv[1], sys.argv[2], float(sys.argv[3])
user_id = f"chaos-recovery-{uuid.uuid4().hex[:8]}"
session_id = f"sess-{uuid.uuid4().hex[:8]}"
query_text = "dress"

def http_json(method, url, payload=None, timeout=8.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        parsed = json.loads(raw) if raw else {}
        return exc.code, parsed

def extract_items(body):
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []

def search():
    params = parse.urlencode({"query": query_text, "userId": user_id, "k": 20, "debug": "false"})
    status, body = http_json("GET", f"{gateway_url}/api/recommendation/search?{params}")
    if status != 200:
        raise RuntimeError(f"search failed with status={status}")
    items = extract_items(body)
    if not items:
        raise RuntimeError("search returned no items")
    return items

def rank_map(items):
    out = {}
    for idx, item in enumerate(items):
        item_id = str(item.get("itemId") or "").strip()
        if item_id:
            out[item_id] = idx
    return out

try:
    baseline = search()
    baseline_rank = rank_map(baseline)
except Exception as exc:
    with open(summary_file, "w", encoding="utf-8") as handle:
        json.dump({"shifted": False, "reason": str(exc)}, handle)
    sys.exit(1)

grouped = {}
for item in baseline:
    item_id = str(item.get("itemId") or "").strip()
    category = str(item.get("category") or "").strip()
    if item_id and category:
        grouped.setdefault(category, []).append(item_id)

if not grouped:
    with open(summary_file, "w", encoding="utf-8") as handle:
        json.dump({"shifted": False, "reason": "no categorized baseline items"}, handle)
    sys.exit(1)

target_category, target_items = max(grouped.items(), key=lambda kv: len(kv[1]))
clicked_items = target_items[: min(3, len(target_items))]
baseline_top10 = [str(item.get("itemId")) for item in baseline[:10]]

for idx in range(6):
    item_id = clicked_items[idx % len(clicked_items)]
    payload = {
        "user_id": user_id,
        "item_id": item_id,
        "session_id": session_id,
        "source": "search",
        "query": query_text,
        "position": baseline_rank.get(item_id, idx),
        "device": "chaos-recovery-probe",
    }
    status, _ = http_json("POST", f"{gateway_url}/api/events/click", payload=payload)
    if status != 200:
        with open(summary_file, "w", encoding="utf-8") as handle:
            json.dump({"shifted": False, "reason": f"click post failed with status={status}"}, handle)
        sys.exit(1)

deadline = time.monotonic() + timeout_sec
last_details = None
while time.monotonic() < deadline:
    after = search()
    after_rank = rank_map(after)
    after_top10 = [str(item.get("itemId")) for item in after[:10]]
    lifts = []
    for item_id in clicked_items:
        if item_id in baseline_rank and item_id in after_rank:
            lifts.append(baseline_rank[item_id] - after_rank[item_id])
    best_lift = max(lifts) if lifts else 0
    shifted = after_top10 != baseline_top10 and best_lift >= 1
    last_details = {
        "shifted": shifted,
        "target_category": target_category,
        "clicked_items": clicked_items,
        "best_lift": best_lift,
        "baseline_top10": baseline_top10,
        "after_top10": after_top10,
    }
    if shifted:
        break
    time.sleep(1.5)

with open(summary_file, "w", encoding="utf-8") as handle:
    json.dump(last_details or {"shifted": False, "reason": "no details"}, handle)

sys.exit(0 if last_details and last_details.get("shifted") else 1)
PYTHON_PROBE
}

test_inference_pod_delete_recovery() {
    log_section "H1. Inference Pod Deletion / Degraded-But-Serving Recovery"
    print_inference_expected_signals

    local desired_replicas
    desired_replicas=$(k8s_get_deployment_replicas "$NAMESPACE" "$INFERENCE_DEPLOYMENT")
    if [[ ! "$desired_replicas" =~ ^[0-9]+$ ]] || [[ "$desired_replicas" -lt 2 ]]; then
        log_warning "Inference deployment has $desired_replicas replica(s); this scenario requires at least 2."
        {
            echo "## H1. Inference Pod Deletion Under Search Traffic"
            echo ""
            echo "**Status**: ⚠️ SKIP"
            echo ""
            echo "- Reason: inference deployment has fewer than 2 replicas."
            echo "- Required action: scale inference to at least 2 replicas before running this scenario."
            echo ""
        } >> "$RESULT_FILE"
        return 0
    fi

    local target_pod
    target_pod=$(k8s_list_pods "$NAMESPACE" "$INFERENCE_LABEL" | head -n 1)
    if [[ -z "$target_pod" ]]; then
        log_error "No inference pod found for deletion"
        return 1
    fi

    local load_output
    load_output=$(mktemp /tmp/inference-chaos-load.XXXXXX.json)

    log_info "Deleting inference pod under live search traffic"
    log_info "  - Target pod: $target_pod"
    log_info "  - Desired replicas: $desired_replicas"
    log_info "  - Load duration: ${INFERENCE_LOAD_DURATION_SEC}s"
    log_info "  - Delete after: ${INFERENCE_DELETE_DELAY_SEC}s"

    local load_pid
    load_pid=$(start_inference_load "$load_output")
    sleep "$INFERENCE_DELETE_DELAY_SEC"

    local delete_started_at
    delete_started_at=$(python3 -c 'import time; print(time.time())')
    k8s_delete_pod "$NAMESPACE" "$target_pod"

    local recovered="false"
    local recovery_finished_at
    recovery_finished_at="$delete_started_at"
    if k8s_wait_for_deployment_replicas "$NAMESPACE" "$INFERENCE_DEPLOYMENT" "$desired_replicas" "$INFERENCE_RECOVERY_TIMEOUT_SEC" 5; then
        recovered="true"
        recovery_finished_at=$(python3 -c 'import time; print(time.time())')
        log_success "Inference deployment returned to $desired_replicas available replicas"
    else
        log_error "Inference deployment did not recover to $desired_replicas available replicas within ${INFERENCE_RECOVERY_TIMEOUT_SEC}s"
    fi

    wait "$load_pid"
    local recovery_seconds
    recovery_seconds=$(python3 - <<PY
start_time = float("$delete_started_at")
end_time = float("$recovery_finished_at")
print(f"{max(0.0, end_time - start_time):.1f}")
PY
)

    if ! analyze_inference_load "$load_output" "$RESULT_FILE" "$INFERENCE_DELETE_DELAY_SEC" "$recovery_seconds" "$recovered"; then
        rm -f "$load_output"
        return 1
    fi

    rm -f "$load_output"
    return 0
}

test_event_consumer_pod_delete_recovery() {
    log_section "H2. Event-Consumer Pod Deletion / Catch-Up Recovery"
    print_consumer_expected_signals

    local desired_replicas
    desired_replicas=$(k8s_get_deployment_replicas "$NAMESPACE" "$CONSUMER_DEPLOYMENT")
    if [[ ! "$desired_replicas" =~ ^[0-9]+$ ]] || [[ "$desired_replicas" -lt 2 ]]; then
        log_warning "Primary event-consumer deployment has $desired_replicas replica(s); this scenario requires at least 2."
        {
            echo "## H2. Event-Consumer Pod Deletion Under Click Traffic"
            echo ""
            echo "**Status**: ⚠️ SKIP"
            echo ""
            echo "- Reason: primary event-consumer deployment has fewer than 2 replicas."
            echo "- Required action: scale event-consumer-primary to at least 2 replicas before running this scenario."
            echo ""
        } >> "$RESULT_FILE"
        return 0
    fi

    local target_pod
    target_pod=$(k8s_list_pods "$NAMESPACE" "$CONSUMER_LABEL" | head -n 1)
    if [[ -z "$target_pod" ]]; then
        log_error "No primary event-consumer pod found for deletion"
        return 1
    fi

    local click_output recovery_probe_output
    click_output=$(mktemp /tmp/consumer-chaos-clicks.XXXXXX.json)
    recovery_probe_output=$(mktemp /tmp/consumer-chaos-probe.XXXXXX.json)
    local user_prefix="chaos-consumer-${TEST_TIMESTAMP}"

    log_info "Deleting primary event-consumer pod under live click traffic"
    log_info "  - Target pod: $target_pod"
    log_info "  - Desired replicas: $desired_replicas"
    log_info "  - Click load duration: ${CONSUMER_LOAD_DURATION_SEC}s"
    log_info "  - Delete after: ${CONSUMER_DELETE_DELAY_SEC}s"

    local click_pid
    click_pid=$(start_click_load "$click_output" "$user_prefix")
    sleep "$CONSUMER_DELETE_DELAY_SEC"

    local delete_started_at
    delete_started_at=$(python3 -c 'import time; print(time.time())')
    k8s_delete_pod "$NAMESPACE" "$target_pod"

    local recovered="false"
    local recovery_finished_at
    recovery_finished_at="$delete_started_at"
    if k8s_wait_for_deployment_replicas "$NAMESPACE" "$CONSUMER_DEPLOYMENT" "$desired_replicas" "$CONSUMER_RECOVERY_TIMEOUT_SEC" 5; then
        recovered="true"
        recovery_finished_at=$(python3 -c 'import time; print(time.time())')
        log_success "Primary event-consumer deployment returned to $desired_replicas available replicas"
    else
        log_error "Primary event-consumer deployment did not recover to $desired_replicas available replicas within ${CONSUMER_RECOVERY_TIMEOUT_SEC}s"
    fi

    wait "$click_pid"
    local click_summary
    click_summary=$(analyze_click_load "$click_output")

    local probe_pass="false"
    if run_consumer_recovery_probe "$recovery_probe_output"; then
        probe_pass="true"
        log_success "Post-recovery personalization probe observed a ranking shift after click feedback"
    else
        log_error "Post-recovery personalization probe did not observe a ranking shift within ${CONSUMER_PROBE_TIMEOUT_SEC}s"
    fi

    local recovery_seconds
    recovery_seconds=$(python3 - <<PY
start_time = float("$delete_started_at")
end_time = float("$recovery_finished_at")
print(f"{max(0.0, end_time - start_time):.1f}")
PY
)

    if ! python3 - "$RESULT_FILE" "$click_summary" "$recovery_probe_output" "$recovered" "$recovery_seconds" "$CONSUMER_MAX_ACK_ERROR_RATE" "$probe_pass" <<'PYTHON_REPORT'
import json
import sys

report_file, click_summary_json, recovery_probe_file, recovered_flag, recovery_seconds, max_error_rate, probe_pass_flag = sys.argv[1:8]
click_summary = json.loads(click_summary_json)
with open(recovery_probe_file, "r", encoding="utf-8") as handle:
    probe = json.load(handle)

recovered = recovered_flag == "true"
probe_pass = probe_pass_flag == "true"
recovery_seconds = float(recovery_seconds)
max_error_rate = float(max_error_rate)

pass_ack_path = click_summary["error_rate"] <= max_error_rate
pass_all = pass_ack_path and recovered and probe_pass

with open(report_file, "a", encoding="utf-8") as handle:
    handle.write("## H2. Event-Consumer Pod Deletion Under Click Traffic\n\n")
    handle.write("**Objective**: Delete one primary event-consumer pod, keep click ingestion live, restore the deployment, and verify post-recovery personalization catch-up through the real click-feedback path.\n\n")
    handle.write("### Expected Operator Signals\n\n")
    handle.write("- Prometheus: `sum(kafka_consumer_lag{topic=\\\"scalestyle.clicks\\\"})` should rise after deletion and return toward baseline.\n")
    handle.write("- Prometheus: `sum(rate(events_processed_total{result=\\\"applied\\\"}[1m]))` should dip during rebalance and recover.\n")
    handle.write("- Alert rules to watch: `KafkaConsumerGroupLagHigh`, `KafkaConsumerGroupLagCritical`.\n")
    handle.write("- Kubernetes: `event-consumer-primary` available replicas should return to the desired count.\n\n")
    handle.write("### Automated Results\n\n")
    handle.write("| Check | Result |\n")
    handle.write("|-------|--------|\n")
    handle.write(f"| Click requests sent | {click_summary['total']} |\n")
    handle.write(f"| Click HTTP 200 responses | {click_summary['successes']} |\n")
    handle.write(f"| Click ack errors | {click_summary['errors']} |\n")
    handle.write(f"| Click ack error rate | {click_summary['error_rate']:.4f} |\n")
    handle.write(f"| Broker-acknowledged clicks | {click_summary['ack_successes']} |\n")
    handle.write(f"| Click ack p99 | {click_summary['p99_ms']:.0f}ms |\n")
    handle.write(f"| Deployment recovered to desired replicas | {'yes' if recovered else 'no'} |\n")
    handle.write(f"| Deployment recovery time | {recovery_seconds:.1f}s |\n")
    handle.write(f"| Post-recovery personalization shift observed | {'yes' if probe_pass else 'no'} |\n\n")
    handle.write("### Recovery Detection\n\n")
    handle.write("Automated recovery is detected when all of the following are true:\n\n")
    handle.write(f"- Click ack error rate <= {max_error_rate:.2f}\n")
    handle.write("- The primary event-consumer deployment returns to its desired available replica count\n")
    handle.write("- A post-recovery click-feedback probe changes personalized search ranking for a dedicated test user\n\n")
    handle.write("### Manual Lag Verification\n\n")
    handle.write("This repository does not ship a ready-to-query Prometheus service endpoint or Kafka admin lag helper for the test shell, so lag verification remains an operator step. Check one of the following:\n\n")
    handle.write("```promql\n")
    handle.write("sum(kafka_consumer_lag{topic=\\\"scalestyle.clicks\\\"})\n")
    handle.write("sum(rate(events_processed_total{result=\\\"applied\\\"}[1m]))\n")
    handle.write("```\n\n")
    handle.write("Alert-backed signals already present in the repo:\n\n")
    handle.write("- `KafkaConsumerGroupLagHigh`\n")
    handle.write("- `KafkaConsumerGroupLagCritical`\n")
    handle.write("- `EventConsumerRetryLagHigh` for retry-tier follow-on pressure\n\n")
    if probe_pass:
        handle.write(f"- Probe details: best clicked-item lift = {probe.get('best_lift', 0)}\n")
    else:
        handle.write(f"- Probe details: {json.dumps(probe, ensure_ascii=True)}\n")
    handle.write("\n")
    handle.write(f"**Status**: {'✅ PASS' if pass_all else '❌ FAIL'}\n\n")

sys.exit(0 if pass_all else 1)
PYTHON_REPORT
    then
        rm -f "$click_output" "$recovery_probe_output"
        return 1
    fi

    rm -f "$click_output" "$recovery_probe_output"
    return 0
}

main() {
    require_tooling
    record_markdown_header

    local all_pass=true

    if [[ "$CHAOS_SCENARIO" == "all" || "$CHAOS_SCENARIO" == "inference" ]]; then
        if ! test_inference_pod_delete_recovery; then
            all_pass=false
        fi
    fi

    if [[ "$CHAOS_SCENARIO" == "all" || "$CHAOS_SCENARIO" == "consumer" ]]; then
        if ! test_event_consumer_pod_delete_recovery; then
            all_pass=false
        fi
    fi

    {
        echo "---"
        echo ""
        echo "## Final Summary"
        echo ""
        echo "- Scenario selection: $CHAOS_SCENARIO"
        echo "- Overall result: $([[ "$all_pass" == "true" ]] && echo "✅ PASS" || echo "❌ FAIL")"
        echo ""
    } >> "$RESULT_FILE"

    if [[ "$all_pass" == "true" ]]; then
        log_section "✅ Chaos validation passed"
        log_info "Results saved to: $RESULT_FILE"
        exit 0
    else
        log_section "❌ Chaos validation failed"
        log_error "Results saved to: $RESULT_FILE"
        exit 1
    fi
}

main "$@"
