from pathlib import Path


def test_render_procfile_uses_asgi_worker():
    procfile = Path(__file__).resolve().parents[1] / "Procfile"

    assert procfile.exists(), "Procfile is required for Render deployment"

    content = procfile.read_text(encoding="utf-8")
    assert "uvicorn" in content.lower(), "Procfile must use uvicorn for the FastAPI app"
