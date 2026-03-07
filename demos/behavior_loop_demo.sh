#!/bin/bash
# Week 2: Behavior Loop Quick Test Script
# 
# Demonstrates real-time personalization for interview recording

set -e

GATEWAY="http://localhost:8080"
USER_ID="demo_user_$(date +%s)"
SESSION_ID="sess_$(uuidgen)"

echo "========================================"
echo "Week 2: Real-time Behavior Loop Demo"
echo "========================================"
echo "User ID: $USER_ID"
echo "Session ID: $SESSION_ID"
echo ""

# Step 1: Search for "dress"
echo "🔍 STEP 1: Searching for 'dress'..."
SEARCH1=$(curl -s "$GATEWAY/api/recommendation/search?query=dress&userId=$USER_ID&k=10&debug=true")
echo "$SEARCH1" | jq -r '.data[:5] | .[] | "\(.articleId) | \(.productGroupName)"'

# Extract top dress item IDs
ITEM1=$(echo "$SEARCH1" | jq -r '.data[0].articleId')
ITEM2=$(echo "$SEARCH1" | jq -r '.data[1].articleId')
ITEM3=$(echo "$SEARCH1" | jq -r '.data[2].articleId')

echo ""
echo "👆 STEP 2: Clicking on 3 dress items..."

# Click item 1
curl -s -X POST "$GATEWAY/api/events/click" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"$USER_ID\",
    \"item_id\": \"$ITEM1\",
    \"session_id\": \"$SESSION_ID\",
    \"source\": \"search\",
    \"query\": \"dress\",
    \"position\": 0
  }" | jq -r '.message'

sleep 0.3

# Click item 2
curl -s -X POST "$GATEWAY/api/events/click" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"$USER_ID\",
    \"item_id\": \"$ITEM2\",
    \"session_id\": \"$SESSION_ID\",
    \"source\": \"search\",
    \"query\": \"dress\",
    \"position\": 1
  }" | jq -r '.message'

sleep 0.3

# Click item 3
curl -s -X POST "$GATEWAY/api/events/click" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"$USER_ID\",
    \"item_id\": \"$ITEM3\",
    \"session_id\": \"$SESSION_ID\",
    \"source\": \"search\",
    \"query\": \"dress\",
    \"position\": 2
  }" | jq -r '.message'

echo ""
echo "⏳ Waiting 3 seconds for feature updater..."
sleep 3

echo ""
echo "🔍 STEP 3: Searching for 'clothes' (expect dress bias)..."
SEARCH2=$(curl -s "$GATEWAY/api/recommendation/search?query=clothes&userId=$USER_ID&k=10&debug=true")
echo "$SEARCH2" | jq -r '.data[:5] | .[] | "\(.articleId) | \(.productGroupName) | boost=\(.boost_reason // "none")"'

echo ""
echo "📊 Analysis:"
DRESS_COUNT=$(echo "$SEARCH2" | jq '[.data[:10] | .[] | select(.productGroupName | contains("Dress"))] | length')
BOOST_COUNT=$(echo "$SEARCH2" | jq '[.data[:10] | .[] | select(.boost_reason != null and .boost_reason != "none")] | length')

echo "   Dress items in top 10: $DRESS_COUNT"
echo "   Items with behavior boost: $BOOST_COUNT"

if [ "$DRESS_COUNT" -ge 3 ] || [ "$BOOST_COUNT" -gt 0 ]; then
  echo ""
  echo "✅ SUCCESS: Behavior loop is working!"
else
  echo ""
  echo "⚠️  PARTIAL: Check event-consumer logs"
fi

echo ""
echo "=========================================="
echo "✅ Demo complete!"
echo "=========================================="
