# ruff: noqa: E402

import grpc
from concurrent import futures
import time
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
proto_dir = os.path.join(current_dir, "src", "proto")
sys.path.append(proto_dir)
from src.config import Config
import recommendation_pb2
import recommendation_pb2_grpc
import logging
import pyarrow.parquet as pq

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("InferenceService")


class RecommendationService(recommendation_pb2_grpc.RecommendationServiceServicer):
    def __init__(self):
        self.top_items = []
        self._load_data()

    def _load_data(self):
        logger.info(f"Attempting to load top items from {Config.DATA_PATH}")
        try:
            if os.path.exists(Config.DATA_PATH):
                table = pq.read_table(Config.DATA_PATH)
                self.top_items = [
                    str(x) for x in table.column("article_id").to_pylist()
                ]
                logger.info(
                    f"Loaded {len(self.top_items)} top items from Parquet file."
                )
            else:
                logger.warning(
                    f"Data path {Config.DEFAULT_DATA_PATH} does not exist. Using fallback data."
                )
                # Fallback: Use sample product IDs instead of crashing
                self.top_items = ["1001", "1002", "1003", "1004", "1005"]
        except Exception as e:
            logger.error(f"Error loading data: {e}. Using fallback data.")
            # Critical fix: Don't return error string as product ID
            # Use fallback sample data instead
            self.top_items = ["1001", "1002", "1003", "1004", "1005"]

    """
    Implementation of the RecommendationService defined in the proto file.
    Provides Top-K product recommendations based on global popularity.
    """

    def GetRecommendations(self, request, context):
        user_id = request.user_id
        k = request.k

        logger.info(
            f"Received recommendation request for user_id: {user_id} with k={k}"
        )

        # Input validation: k should be positive
        if k <= 0:
            logger.warning(f"Invalid k value: {k}. Using default k=10.")
            k = 10

        # Input validation: k should not exceed available items
        if k > len(self.top_items):
            logger.warning(
                f"Requested k={k} exceeds available items ({len(self.top_items)}). "
                f"Returning all {len(self.top_items)} items."
            )
            k = len(self.top_items)

        recommendations = self.top_items[:k]

        response = recommendation_pb2.RecommendationResponse()
        response.user_id = user_id

        for item_id in recommendations:
            item = response.items.add()
            item.item_id = item_id
            item.score = 0.95  # Placeholder score (Phase 1 baseline)

        logger.info(
            f"Returning {len(recommendations)} recommendations for user_id: {user_id}"
        )
        return response


def serve():
    # Start the gRPC server with a thread pool
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    # Register our service implementation with the server
    recommendation_pb2_grpc.add_RecommendationServiceServicer_to_server(
        RecommendationService(), server
    )

    # Listen on port 50051
    port = Config.PORT
    server.add_insecure_port(f"[::]:{port}")
    print(f"Starting server on port {port}...")
    server.start()

    try:
        # Keep the main thread running until interrupted by Ctrl+C
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("Stopping server...")
        server.stop(0)


if __name__ == "__main__":
    serve()
