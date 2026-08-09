#!/usr/bin/env python3
def test_health_check(client):
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert "status" in response.json()
    assert response.json()["status"] == "ok"


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__]))

