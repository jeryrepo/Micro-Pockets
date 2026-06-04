import asyncio
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

async def main():
    client = AsyncIOMotorClient(
        os.getenv("MONGODB_URI")
    )

    print(await client.admin.command("ping"))

asyncio.run(main())