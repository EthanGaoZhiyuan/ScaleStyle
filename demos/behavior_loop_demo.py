#!/usr/bin/env python3
"""
Week 2: Real-time Behavior Loop Test

Validates that user clicks immediately affect next search/recommendation results.

Test Flow:
1. User searches for "dress" → Gets initial results
2. User clicks on multiple dress items (category: Dress)
3. User searches again for "clothes" → Results should be biased towards Dress category
4. Verify behavior boost in debug output

Demo-ready for interview recording.
"""
import requests
import time
import uuid
import json
from typing import List, Dict

# Configuration
GATEWAY_URL = "http://localhost:8080"
USER_ID = f"test_user_{int(time.time())}"
SESSION_ID = str(uuid.uuid4())

def search(query: str, user_id: str = None, k: int = 10, debug: bool = True) -> Dict:
    """Execute search query"""
    params = {
        "query": query,
        "k": k,
        "debug": str(debug).lower()
    }
    if user_id:
        params["userId"] = user_id
    
    print(f"\n🔍 Searching: query='{query}' user_id={user_id}")
    resp = requests.get(f"{GATEWAY_URL}/api/recommendation/search", params=params)
    resp.raise_for_status()
    return resp.json()

def track_click(
    user_id: str, 
    item_id: str, 
    session_id: str, 
    source: str = "search", 
    query: str = None,
    position: int = None
) -> Dict:
    """Track click event"""
    payload = {
        "user_id": user_id,
        "item_id": item_id,
        "session_id": session_id,
        "source": source,
        "query": query,
        "position": position
    }
    
    print(f"👆 Click: user_id={user_id} item_id={item_id} pos={position}")
    resp = requests.post(f"{GATEWAY_URL}/api/events/click", json=payload)
    resp.raise_for_status()
    return resp.json()

def extract_top_items(results: Dict, top_n: int = 5) -> List[Dict]:
    """Extract top N items from search results"""
    items = results.get("data", [])
    return items[:top_n]

def print_results(results: Dict, title: str = "Results"):
    """Pretty print search results"""
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")
    
    items = results.get("data", [])
    for i, item in enumerate(items[:10], 1):
        article_id = item.get("articleId")
        category = item.get("productGroupName", "N/A")
        color = item.get("colourGroupName", "N/A")
        name = item.get("prodName", "N/A")
        score = item.get("score", 0.0)
        boost = item.get("boost_reason", "none")
        
        print(f"{i:2}. {article_id} | {category:15} | {color:12} | score={score:.4f} | boost={boost}")
    
    # Print debug info if available
    if "latency_ms" in results:
        print(f"\n⏱️  Latency: {results['latency_ms'].get('total', 0):.1f}ms")
    if "contract" in results:
        print(f"📋 Contract: {results['contract']}")

def main():
    print("="*60)
    print("🧪 Week 2: Real-time Behavior Loop Test")
    print("="*60)
    print(f"User ID: {USER_ID}")
    print(f"Session ID: {SESSION_ID}")
    print("="*60)
    
    # Step 1: Initial search for "dress"
    print("\n📍 STEP 1: Initial search for 'dress'")
    results_1 = search("dress", user_id=USER_ID, k=10, debug=True)
    print_results(results_1, title="Initial Search Results (dress)")
    
    top_items = extract_top_items(results_1, top_n=5)
    dress_items = [item for item in top_items if "dress" in item.get("productGroupName", "").lower()]
    
    if not dress_items:
        print("\n⚠️  Warning: No dress items found in top results. Using top items instead.")
        dress_items = top_items[:3]
    else:
        dress_items = dress_items[:3]  # Click top 3 dress items
    
    print(f"\n✅ Found {len(dress_items)} dress items to click")
    
    # Step 2: Simulate user clicking multiple dress items
    print("\n📍 STEP 2: User clicks on 3 dress items")
    for i, item in enumerate(dress_items):
        track_click(
            user_id=USER_ID,
            item_id=item.get("articleId"),
            session_id=SESSION_ID,
            source="search",
            query="dress",
            position=i
        )
        time.sleep(0.2)  # Small delay between clicks
    
    # Wait for Kafka consumer to process events
    print("\n⏳ Waiting 3 seconds for feature updater to process events...")
    time.sleep(3)
    
    # Step 3: Search again with different query (should see dress bias)
    print("\n📍 STEP 3: Search for 'clothes' (expect dress category bias)")
    results_2 = search("clothes", user_id=USER_ID, k=10, debug=True)
    print_results(results_2, title="After Clicks Search Results (clothes)")
    
    # Step 4: Analyze results
    print("\n📊 Analysis:")
    print("-" * 60)
    
    # Count dress category in top 10
    items_after = results_2.get("data", [])[:10]
    dress_count_after = sum(1 for item in items_after if "dress" in item.get("productGroupName", "").lower())
    boosted_count = sum(1 for item in items_after if item.get("boost_reason") not in [None, "none"])
    
    print(f"Dress items in top 10 (after clicks): {dress_count_after}/10")
    print(f"Items with behavior boost: {boosted_count}/10")
    
    # Check if behavior loop is working
    if dress_count_after >= 3 or boosted_count > 0:
        print("\n✅ SUCCESS: Behavior loop is working!")
        print("   → User clicks on dress items → Next search biased towards dress")
    else:
        print("\n⚠️  PARTIAL: Behavior loop might need tuning")
        print("   → Check event-consumer logs and Redis keys")
    
    # Step 5: Verify Redis keys (optional, requires redis-cli)
    print("\n📋 Redis Keys to Check:")
    print(f"   redis-cli LRANGE user:{USER_ID}:recent_clicks 0 -1")
    print(f"   redis-cli KEYS user:{USER_ID}:category_affinity:*")
    print(f"   redis-cli ZRANGE global:popular 0 10 WITHSCORES")
    
    print("\n" + "="*60)
    print("🎉 Test completed! Check logs above for behavior boost evidence.")
    print("="*60)

if __name__ == "__main__":
    main()
