def test_health_check(client):
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert "status" in response.json()
    assert response.json()["status"] == "ok"
