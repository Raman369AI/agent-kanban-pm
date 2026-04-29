import sys, os
sys.path.append(os.getcwd())

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient
from main import app

def test_phase2():
    with TestClient(app) as client:
        print("=== List entities after adapter sync ===")
        r = client.get("/entities?entity_type=agent")
        print(f"Status: {r.status_code}, Count: {len(r.json())}")
        agents = r.json()
        for a in agents:
            print(f"  - {a['name']}: role={a['role']}, skills={a.get('skills')}")

        # Check that bundled adapters created entities
        # Note: adapters whose CLI tool is not installed are marked inactive
        names = {a["name"] for a in agents}
        expected = {"claude", "gemini", "opencode"}  # codex not installed in this env
        found = expected & names
        print(f"\nExpected adapters: {expected}")
        print(f"Found adapters: {found}")
        assert found == expected, f"Missing adapters: {expected - found}"

        # Check roles (Entity.name now stores adapter 'name', not 'display_name')
        claude = next((a for a in agents if a["name"] == "claude"), None)
        assert claude is not None and claude["role"] == "manager", "Claude should be manager"

        gemini = next((a for a in agents if a["name"] == "gemini"), None)
        assert gemini is not None and gemini["role"] == "worker", "Gemini should be worker"

        print("\n=== PHASE 2 TESTS PASSED ===")

if __name__ == "__main__":
    test_phase2()
