# SimuMarketAI Backend

Backend API, job orchestration, deterministic engines, dan integrasi OASIS untuk **SimuMarketAI**.

## Status

Fondasi Fase 0 tersedia pada branch `dev`: aplikasi FastAPI, konfigurasi environment,
PostgreSQL, Redis, Alembic, correlation ID, bentuk error stabil, health/readiness,
Docker Compose, test dasar, dan CI. Domain bisnis serta integrasi OASIS belum masuk
jalur aplikasi.

## Tanggung jawab repository

- FastAPI API untuk autentikasi, edukasi, market analysis, transaksi, dan report.
- PostgreSQL sebagai system of record; `pgvector` baru dipakai ketika retrieval benar-benar masuk roadmap.
- Redis + Celery untuk job simulasi yang berdurasi panjang.
- Context builder dan data provenance.
- Adapter OASIS/CAMEL-AI untuk panel persona sintetis.
- Orkestrasi empat agent inti berbasis OASIS beserta personality council masing-masing.
- Financial engine dan Launch Readiness Score yang deterministik.
- Pipeline upload foto struk, OCR/extraction, confidence per field, review, dan konversi ke draft transaksi.
- Report synthesis dengan schema validation dan citation guard.
- Audit log, observability, retry, timeout, dan cost budget LLM.

## Stack target

- Python 3.11
- FastAPI + Pydantic
- PostgreSQL
- Redis + Celery
- `camel-oasis` / CAMEL-AI
- Google Gemini atau OpenAI melalui model adapter CAMEL, dipilih per deployment

OASIS `0.2.5` pada branch utama yang diaudit mensyaratkan Python `>=3.10,<3.12`, sehingga Python 3.11 sesuai. Versi dependency harus dipin dan diuji ulang sebelum implementasi.

## Batas penting

Keempat jenis agent inti dijalankan melalui orkestrasi OASIS. Finance Agent wajib memakai deterministic financial tool untuk semua angka; OASIS mengatur personality, interaksi, kritik, dan sintesisnya. Skor, BEP, margin, serta payback period tetap dihitung oleh kode deterministik agar dapat diaudit.

## Rencana struktur

```text
app/
  api/             # routers dan DTO
  core/            # config, security, observability
  domains/         # education, analysis, transaction, report
  engines/         # scoring dan finance deterministik
  integrations/    # OASIS, LLM provider, OCR, data provider
  workers/         # Celery tasks
  persistence/     # models dan repositories
tests/
```

## Dokumen acuan

- [Arsitektur sistem](https://github.com/NextGen-Manager/Docs/blob/main/docs/02-system-architecture.md)
- [Desain agent OASIS](https://github.com/NextGen-Manager/Docs/blob/main/docs/03-oasis-agent-architecture.md)
- [Protokol simulasi](https://github.com/NextGen-Manager/Docs/blob/main/docs/04-simulation-protocol.md)
- [Data dan scoring](https://github.com/NextGen-Manager/Docs/blob/main/docs/05-data-evidence-and-scoring.md)
- [Kontrak API](https://github.com/NextGen-Manager/Docs/blob/main/docs/06-api-contract.md)
- [Database dan storage](https://github.com/NextGen-Manager/Docs/blob/main/docs/10-database-and-storage-design.md)
- [Deployment dan observability](https://github.com/NextGen-Manager/Docs/blob/main/docs/11-deployment-and-observability.md)

## Menjalankan layanan

Prasyarat: Docker Desktop, atau Python 3.11 dan `uv` untuk menjalankan tanpa container.

### Docker Compose

```bash
docker compose up --build -d
docker compose exec api uv run alembic upgrade head
```

API tersedia di `http://localhost:8000`. Pemeriksaan:

```bash
curl http://localhost:8000/v1/health
curl http://localhost:8000/v1/ready
```

Hentikan service tanpa menghapus volume database:

```bash
docker compose down
```

### Lokal

```bash
uv sync --all-groups
copy .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

### Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

Jangan mengisi atau melakukan commit terhadap `.env`. Pilih provider dan model
melalui `OASIS_PROVIDER` serta `OASIS_MODEL_ID`. Isi hanya `GEMINI_API_KEY` atau
`OPENAI_API_KEY` milik provider yang dipilih ketika integrasi live dijalankan.
