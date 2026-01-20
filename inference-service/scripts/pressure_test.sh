#!/bin/bash
# Pressure test script for ScaleStyle inference service
# Tests async performance with concurrent requests

set -e

# Configuration
BASE_URL="${BASE_URL:-http://localhost:8000}"
CONCURRENT="${CONCURRENT:-20}"
DURATION="${DURATION:-30}"
OUTPUT_DIR="./test_results"

echo "========================================="
echo "ScaleStyle Pressure Test"
echo "========================================="
echo "Base URL: $BASE_URL"
echo "Concurrent requests: $CONCURRENT"
echo "Duration: ${DURATION}s"
echo ""

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Test queries
QUERIES=(
    "red dress under 50"
    "blue jeans"
    "winter coat"
    "running shoes"
    "summer dress"
    "casual shirt"
    "black pants"
    "white sneakers"
)

# Function to send single request
send_request() {
    local query=$1
    local user_id=$2
    local start_time=$(date +%s%3N)
    
    response=$(curl -s -w "\n%{http_code}\n%{time_total}" -X POST "$BASE_URL/search" \
        -H "Content-Type: application/json" \
        -d "{\"query\":\"$query\",\"k\":10,\"debug\":true,\"user_id\":\"$user_id\"}")
    
    local end_time=$(date +%s%3N)
    local latency=$((end_time - start_time))
    
    # Extract HTTP status and time
    local http_code=$(echo "$response" | tail -n 2 | head -n 1)
    local curl_time=$(echo "$response" | tail -n 1)
    
    echo "$query,$user_id,$http_code,$latency,$curl_time" >> "$OUTPUT_DIR/results_$$.csv"
}

# Initialize results file
echo "query,user_id,http_code,latency_ms,curl_time" > "$OUTPUT_DIR/results_$$.csv"

# Run concurrent test
echo "Starting pressure test..."
start_test=$(date +%s)
end_test=$((start_test + DURATION))

request_count=0
while [ $(date +%s) -lt $end_test ]; do
    # Launch concurrent requests
    for i in $(seq 1 $CONCURRENT); do
        query_idx=$((RANDOM % ${#QUERIES[@]}))
        query="${QUERIES[$query_idx]}"
        user_id="user$((RANDOM % 100))"
        
        send_request "$query" "$user_id" &
        request_count=$((request_count + 1))
    done
    
    # Wait for batch to complete
    wait
    
    echo "Completed $request_count requests..."
done

echo ""
echo "========================================="
echo "Test completed!"
echo "Total requests: $request_count"
echo ""

# Analyze results
if command -v python3 &> /dev/null; then
    echo "Analyzing results..."
    python3 << 'EOF'
import pandas as pd
import sys

try:
    # Read results
    df = pd.read_csv(sys.argv[1])
    
    # Calculate statistics
    print("\n=== Latency Statistics (ms) ===")
    print(f"Min: {df['latency_ms'].min():.2f}")
    print(f"Max: {df['latency_ms'].max():.2f}")
    print(f"Mean: {df['latency_ms'].mean():.2f}")
    print(f"Median: {df['latency_ms'].median():.2f}")
    print(f"P95: {df['latency_ms'].quantile(0.95):.2f}")
    print(f"P99: {df['latency_ms'].quantile(0.99):.2f}")
    
    # HTTP status breakdown
    print("\n=== HTTP Status Codes ===")
    print(df['http_code'].value_counts())
    
    # Find slow requests
    print("\n=== Slowest 10 Requests ===")
    slow = df.nlargest(10, 'latency_ms')[['query', 'latency_ms', 'http_code']]
    print(slow.to_string(index=False))
    
    # Success rate
    success_rate = (df['http_code'] == 200).sum() / len(df) * 100
    print(f"\n=== Success Rate: {success_rate:.2f}% ===")
    
except Exception as e:
    print(f"Analysis failed: {e}")
EOF
    python3 $(dirname $0)/analyze_results.py "$OUTPUT_DIR/results_$$.csv"
else
    echo "Python3 not found. Raw results saved to: $OUTPUT_DIR/results_$$.csv"
    echo "Use: cat $OUTPUT_DIR/results_$$.csv | awk -F',' '{sum+=\$4; count++} END {print \"Avg latency:\", sum/count, \"ms\"}'"
fi

echo ""
echo "Results saved to: $OUTPUT_DIR/results_$$.csv"
