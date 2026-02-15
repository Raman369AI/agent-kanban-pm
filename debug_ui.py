
import asyncio
import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

from httpx import AsyncClient
from main import app

async def debug_request():
    print("Testing GET /ui/projects/2/board...")
    async with AsyncClient(app=app, base_url="http://test") as ac:
        try:
            response = await ac.get("/ui/projects/2/board")
            print(f"Status Code: {response.status_code}")
            if response.status_code != 200:
                print("Response Text (first 500 chars):")
                print(response.text[:500])
        except Exception as e:
            print(f"Request failed with exception: {e}")

if __name__ == "__main__":
    asyncio.run(debug_request())
