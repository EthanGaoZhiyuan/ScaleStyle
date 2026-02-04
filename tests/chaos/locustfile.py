"""
Locust load test for Chaos Engineering validation.
Usage: locust -f infrastructure/k8s/chaos/locustfile.py --host=http://localhost:8080
"""

from locust import HttpUser, task, between
import random


class ScaleStyleUser(HttpUser):
    """Simulates user search behavior during chaos testing."""

    wait_time = between(0.1, 0.5)  # 100-500ms between requests

    queries = [
        "red dress",
        "black jeans",
        "white shirt",
        "blue sweater",
        "summer dress",
    ]

    @task(10)
    def search(self):
        """Primary search endpoint - most traffic."""
        query = random.choice(self.queries)
        k = random.randint(3, 10)

        with self.client.get(
            f"/api/recommendation/search?query={query}&k={k}",
            catch_response=True,
            name="/api/recommendation/search",
        ) as response:
            if response.status_code == 200:
                data = response.json()

                # Check if degraded mode
                if data.get("data") and len(data["data"]) > 0:
                    first_item = data["data"][0]
                    if first_item.get("degraded"):
                        response.failure(
                            f"Degraded mode: source={first_item.get('source')}"
                        )
                    elif first_item.get("source") != "ray":
                        response.failure(
                            f"Not using Ray: source={first_item.get('source')}"
                        )
                    else:
                        response.success()
                else:
                    response.failure("Empty results")
            else:
                response.failure(f"HTTP {response.status_code}")

    @task(2)
    def popular(self):
        """Fallback popular endpoint."""
        with self.client.get(
            "/api/recommendation/popular?k=10",
            catch_response=True,
            name="/api/recommendation/popular",
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}")

    @task(1)
    def health(self):
        """Health check endpoint."""
        self.client.get("/actuator/health", name="/actuator/health")
