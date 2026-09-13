import asyncio

from .db import SessionLocal, create_schema
from .jobs import worker_loop
from .seed import seed


async def main():
    create_schema()
    with SessionLocal() as db:
        seed(db)
    stop = asyncio.Event()
    await asyncio.gather(worker_loop(stop, ["asr"]), worker_loop(stop, ["suggestions", "fact_extraction"]))


if __name__ == "__main__":
    asyncio.run(main())
