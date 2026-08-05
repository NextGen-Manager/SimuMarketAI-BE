# SimuMarketAI Backend

Backend API, job orchestration, deterministic engines, dan integrasi OASIS untuk **SimuMarketAI**.

## Status

Repository ini baru diinisialisasi. Belum ada source code layanan. Desain teknis dan batas penggunaan OASIS tersedia di repository [Docs](https://github.com/NextGen-Manager/Docs).

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
- Google Gemini melalui model adapter CAMEL, dengan provider fallback di belakang interface internal

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

Belum tersedia. Ketika scaffold dibuat, README ini harus memuat setup lokal, migrasi database, worker, seed data, test, lint, serta seluruh environment variables tanpa menyertakan secret asli.
