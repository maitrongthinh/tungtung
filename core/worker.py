from __future__ import annotations

from redis import Redis
from rq import Worker

from common.queue import get_redis_connection


def main() -> None:
    connection: Redis = get_redis_connection()
    worker = Worker(["crawl", "analysis", "publish", "memory"], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
