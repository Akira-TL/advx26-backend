# OpenAPI

`openapi.yaml` is the committed OpenAPI 3.1 contract for the Cloud Media Service.
It is generated from the same FastAPI routes and Pydantic models used by the running service.

## Regenerate

```bash
python scripts/export_openapi.py
```

To embed a deployed public endpoint:

```bash
python scripts/export_openapi.py --server-url https://media.example.com
```

CI or local consistency check:

```bash
python scripts/export_openapi.py --check
```

Runtime documentation remains available at:

- `/docs` — Swagger UI
- `/redoc` — ReDoc
- `/openapi.json` — runtime JSON schema

The committed YAML uses `http://127.0.0.1:9000` so it is deterministic across machines. Production deployments should set `BACKEND_PUBLIC_BASE_URL` and may export a deployment-specific copy without committing secrets or device Tokens.
