from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz(make_settings):
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "repo": "home-assistant/core",
        "data_source": "fixtures",
        "llm_mode": "recorded",
    }


def test_unhandled_errors_return_generic_body(make_settings):
    app = create_app(make_settings())

    @app.get("/boom")
    async def boom():
        raise RuntimeError("upstream said: secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret internal detail" not in response.text
