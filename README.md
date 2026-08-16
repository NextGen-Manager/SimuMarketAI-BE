# SimuMarketAI Backend

Backend API, deterministic decision engine, asynchronous job orchestration, dan integrasi OASIS untuk SimuMarket AI.

**[Cara menjalankan](#cara-menjalankan)** · [Tech stack](#tech-stack) · [Arsitektur](#arsitektur) · [Konfigurasi](#konfigurasi) · [Quality checks](#quality-checks) · [Dokumentasi teknis](https://github.com/NextGen-Manager/Docs)

## Tentang repository

Repository ini menyediakan:

- REST API FastAPI untuk autentikasi, usaha, edukasi, analysis, produk, transaksi, receipt, dan export;
- PostgreSQL sebagai system of record dan Redis sebagai broker serta koordinasi job;
- Celery worker untuk simulasi OASIS, OCR, export PDF, retention, dan recovery;
- engine finansial, scoring, analytics, dan report validation yang deterministik;
- empat council OASIS untuk market analysis, customer persona, finance review, dan report review;
- provider Gemini atau OpenAI yang dipilih secara eksplisit per deployment;
- private object storage S3-compatible untuk struk dan hasil export;
- provenance, correlation ID, idempotency, serta failure state yang dapat diaudit.

LLM tidak menjadi sumber angka otoritatif. Skor, BEP, marjin, payback, dan agregat transaksi selalu dihitung oleh kode deterministik. Kegagalan agent tidak ditutupi dengan nilai bawaan dan dapat menghasilkan laporan berstatus `partial`.

## Cara menjalankan

### Docker Compose

Cara paling sederhana menjalankan API beserta PostgreSQL, Redis, MinIO, worker, dan scheduler:

```bash
git clone https://github.com/NextGen-Manager/SimuMarketAI-BE.git
cd SimuMarketAI-BE
docker compose up --build -d
docker compose exec api uv run alembic upgrade head
```

Endpoint lokal:

- API: `http://localhost:8000`
- OpenAPI: `http://localhost:8000/docs`
- Health: `http://localhost:8000/v1/health`
- Readiness: `http://localhost:8000/v1/ready`
- MinIO Console: `http://localhost:9001`

Hentikan service tanpa menghapus volume:

```bash
docker compose down
```

### Menjalankan API secara lokal

Prasyarat:

- Python 3.11;
- [uv](https://docs.astral.sh/uv/);
- PostgreSQL, Redis, dan object storage, atau service pendukung dari Docker Compose.

```bash
docker compose up -d postgres redis minio
uv sync --all-groups
cp .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Pada PowerShell gunakan `Copy-Item .env.example .env` untuk menyalin konfigurasi. Jangan commit `.env` atau kredensial provider.

Tanpa API key, pipeline tetap berjalan dengan adapter unavailable dan menyatakan bagian simulasi tidak tersedia. Sistem tidak membuat output agent palsu.

## Tech stack

| Bagian | Teknologi |
|---|---|
| Runtime | Python 3.11 |
| API | FastAPI, Pydantic 2, Uvicorn |
| Persistence | SQLAlchemy 2, PostgreSQL 16, pgvector, Alembic |
| Queue dan cache | Redis 7, Celery 5 |
| Multi-agent | `camel-oasis` 0.2.5 dan CAMEL-AI |
| LLM provider | Google Gemini atau OpenAI melalui CAMEL model adapter |
| OCR | PaddleOCR PP-StructureV3 |
| Object storage | S3-compatible storage, MinIO untuk lokal |
| PDF | ReportLab |
| Quality | Ruff, mypy, pytest, pip-audit |

Runtime OASIS dipasang hanya pada worker melalui optional dependency `oasis`. API process tidak membutuhkan model stack tersebut.

## Arsitektur

```text
app/
  api/             router, dependency, dan DTO HTTP
  core/            config, security, logging, dan correlation ID
  domain/          kontrak evidence, agent, artifact, dan state
  engines/         finance, scoring, analytics, dan report deterministik
  integrations/    OASIS, evidence provider, OCR, dan object storage
  repositories/    akses data tenant-scoped
  schemas/         request dan response models
  services/        use case serta orchestration
  workers/         Celery worker dan scheduler
migrations/         revision Alembic
tests/              unit, integration, contract, security, dan recovery tests
```

Alur analysis utama:

```text
queued -> collecting_evidence -> building_context -> simulating
  -> calculating_finance -> scoring -> composing_report
  -> validating_report -> completed | partial | failed | cancelled
```

OASIS menangani interaksi dan kritik agent. Seluruh hasil numerik yang masuk laporan berasal dari engine deterministik dan harus memiliki provenance.

## Konfigurasi

Salin `.env.example` menjadi `.env`, lalu sesuaikan nilai lokal. Kelompok konfigurasi utama:

| Variabel | Fungsi |
|---|---|
| `DATABASE_URL` | koneksi PostgreSQL |
| `REDIS_URL` | Redis API dan default Celery |
| `JWT_SECRET` | signing secret autentikasi |
| `CORS_ORIGINS` | allowlist origin frontend |
| `OASIS_PROVIDER` | `gemini` atau `openai` |
| `OASIS_MODEL_ID` | model yang sesuai dengan provider |
| `GEMINI_API_KEY` | kredensial Gemini bila provider Gemini dipilih |
| `OPENAI_API_KEY` | kredensial OpenAI bila provider OpenAI dipilih |
| `OBJECT_STORAGE_*` | endpoint, bucket, region, dan kredensial storage |
| `RECEIPT_OCR_*` | provider serta versi pipeline OCR |

Provider, model, dan key divalidasi saat startup. Sistem tidak melakukan fallback diam-diam ke key provider lain.

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
uv run alembic check
```

CI juga menjalankan dependency audit, migrasi pada PostgreSQL 16, backup/restore terisolasi, pemeriksaan revision Alembic, serta bounded readiness load smoke.

## Dokumentasi

- [Dokumentasi SimuMarket AI](https://github.com/NextGen-Manager/Docs)
- [Arsitektur sistem](https://github.com/NextGen-Manager/Docs/blob/main/docs/02-system-architecture.md)
- [Arsitektur agent OASIS](https://github.com/NextGen-Manager/Docs/blob/main/docs/03-oasis-agent-architecture.md)
- [Data, evidence, dan scoring](https://github.com/NextGen-Manager/Docs/blob/main/docs/05-data-evidence-and-scoring.md)
- [Kontrak API](https://github.com/NextGen-Manager/Docs/blob/main/docs/06-api-contract.md)
- [Keamanan dan AI safety](https://github.com/NextGen-Manager/Docs/blob/main/docs/07-security-privacy-ai-safety.md)
- [Deployment dan observability](https://github.com/NextGen-Manager/Docs/blob/main/docs/11-deployment-and-observability.md)
