import os


class Config:
    DEFAULT_DATA_PATH = "../data-pipeline/data/processed/"
    DATA_PATH = os.getenv("DATA_PATH", DEFAULT_DATA_PATH)
    PORT = 50051
