import os

import uvicorn

if __name__ == "__main__":
    # Enable the in-process media pipeline worker by default for local runs.
    os.environ.setdefault("BACKEND_WORKER_ENABLED", "1")
    # Dev-only fixed device tokens so /ready passes and NFC endpoints work locally.
    os.environ.setdefault("BACKEND_TRIGGER_TOKEN", "dev-trigger-token")
    os.environ.setdefault("BACKEND_PLAYBACK_TOKEN", "dev-playback-token")
    uvicorn.run("app.main:app", host="0.0.0.0", port=9000, reload=False)
