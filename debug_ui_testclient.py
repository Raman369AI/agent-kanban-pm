
import os
import sys

# Add current dir to path
sys.path.append(os.getcwd())

from fastapi.testclient import TestClient
from main import app

def test_ui_render():
    print("Initializing TestClient...")
    with TestClient(app) as client:
        print("Sending GET request to /ui/projects/1/board")
        try:
            response = client.get("/ui/projects/1/board")
            print(f"Status Code: {response.status_code}")
            if response.status_code == 500:
                print("Internal Server Error detected.")
                print("Response text: ", response.text)
            else:
                print("Success!")
        except Exception as e:
            print(f"Caught exception: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    test_ui_render()
