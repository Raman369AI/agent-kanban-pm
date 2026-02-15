
import asyncio
import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

from httpx import AsyncClient, ASGITransport
from main import app
from database import init_db

async def run_debug():
    print("Initing DB...")
    await init_db()
    
    print("Running request...")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        try:
            response = await ac.get("/ui/projects/1/board")
            print(f"Status: {response.status_code}")
            if response.status_code == 500:
                print("Response text: ", response.text)
        except Exception as e:
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_debug())
