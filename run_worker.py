from series_manager.db import connect_mongodb
from series_manager.jobs.worker import worker_loop


if __name__ == "__main__":
    connect_mongodb()
    worker_loop()
