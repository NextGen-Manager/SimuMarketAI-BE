# Backend — Coding Rules

Aturan untuk repository backend SimuMarket AI.

## Lima aturan yang berlaku di seluruh produk

SimuMarket AI terdiri dari tiga repository terpisah: [Docs](https://github.com/NextGen-Manager/Docs) sebagai sumber kebenaran, [SimuMarketAI](https://github.com/NextGen-Manager/SimuMarketAI) untuk frontend, dan repository ini untuk backend. Lima aturan berikut berlaku di ketiganya. Melanggarnya membatalkan klaim inti produk, bukan sekadar melanggar gaya.

1. **LLM tidak pernah menjadi sumber angka otoritatif.** Skor, BEP, marjin, payback, dan seluruh agregat dihitung kode deterministik. Agent boleh mengkritik angka, tidak boleh membuatnya.
2. **Setiap angka yang tampil punya provenance.** Nilai, satuan, sumber, waktu pengambilan, dan tingkat keyakinan.
3. **Kegagalan parsial tidak boleh disamarkan.** Status `partial` tetap `partial`. Komponen yang gagal tidak diberi nilai bawaan.
4. **Uang adalah integer rupiah.** Tidak ada `float` di jalur uang, di bahasa mana pun.
5. **Data pengguna tidak bocor ke prompt.** Nama pelanggan, nomor telepon, teks struk mentah, dan catatan bebas tidak pernah dikirim ke penyedia LLM.

Kalau sebuah tugas tampak menuntut pelanggaran salah satu di atas, berhenti dan tanyakan — jangan cari jalan pintas.

**Bahasa.** Teks yang dilihat pengguna Bahasa Indonesia, termasuk pesan error. Kode, nama variabel, nama file, dan commit message Bahasa Inggris. Komentar kode menjelaskan *kenapa*, bukan *apa*.

**Git.** Jangan commit atau push kecuali diminta. Jangan pernah commit `.env`, kunci API, dump database, trace OASIS, PDF milik pengguna, atau foto struk. Satu commit satu perubahan logis.

**Dokumen menang atas kode.** Kalau kode menyimpang dari `Docs`, yang salah adalah kode — kecuali ada ADR yang menyatakan sebaliknya.

## Batas repository ini

Backend adalah tempat seluruh angka otoritatif lahir. Karena itu batasnya ketat:

- **Engine deterministik** menghitung finance dan score. Selalu.
- **OASIS agent** berdebat, mengkritik, dan menyusun narasi. Tidak pernah menghitung.
- **Evidence adapter** mengambil dan menormalkan data eksternal, lengkap dengan provenance.

Kalau sebuah angka bisa ditelusuri ke teks yang digenerate LLM, itu bug — sekalipun angkanya kebetulan benar.

## Stack

| Hal | Pin | Catatan |
|---|---|---|
| Python | **3.11** | dikunci oleh `camel-oasis`, bukan preferensi |
| FastAPI | 0.136.x | |
| Pydantic | 2.x | |
| PostgreSQL | 16 + pgvector | pgvector disiapkan, belum dipakai di MVP |
| Redis + Celery | 7 / 5.x | |
| `camel-oasis` | 0.2.5 | rilis terakhir Des 2025, vendor dan pin ke commit |
| `camel-ai` | 0.2.78 | |
| LLM default | `gemini-3.1-flash-lite` | model `-preview` dilarang untuk jalur demo |
| OCR | PaddleOCR PP-StructureV3 | |

**Jangan menaikkan Python ke 3.12+.** OASIS 0.2.5 mensyaratkan `<3.12`. Upgrade akan merusak simulasi. Alasan lengkap di `Docs/docs/14-tech-stack-decisions.md`.

## Struktur

```text
app/
  api/            router dan DTO
  core/           config, security, observability
  domains/        education, analysis, transaction, report
  engines/        finance dan scoring deterministik
  integrations/   OASIS, LLM provider, OCR, data provider
  workers/        Celery tasks
  persistence/    model dan repository
tests/
```

Aturan struktur:

- `engines/` **tidak boleh** mengimpor apa pun dari `integrations/`. Engine harus dapat diuji tanpa jaringan dan tanpa LLM.
- `api/` tidak memanggil `integrations/` langsung; lewat `domains/`.
- `domains/` tidak menulis SQL mentah; lewat `persistence/`.

## Uang dan aritmetika

```python
# benar
from decimal import Decimal
contribution_margin = selling_price - variable_cost   # int rupiah

# salah
margin = price * 0.35   # float di jalur uang
```

- Uang selalu `int` rupiah atau `Decimal`. **Tidak pernah `float`.**
- Validasi non-negatif dan kesamaan currency sebelum menghitung.
- Bila `contribution_margin <= 0`, BEP dan payback **tidak terdefinisi** — kembalikan penanda eksplisit beserta warning, jangan nol dan jangan infinity.
- Bila `monthly_operating_profit <= 0`, payback tidak terdefinisi.
- Setiap hasil finansial menyebutkan apa yang included dan excluded (pajak, depresiasi, gaji pemilik, fee platform, promo, spoilage).

Formula otoritatif ada di `Docs/docs/05-data-evidence-and-scoring.md`. Jangan menulis ulang formula dari ingatan.

## Scoring

- Seluruh rule versioned. Perubahan bobot atau ambang membuat versi baru, tidak menimpa yang lama.
- Report lama tetap menunjuk versi rule lama.
- `rule_version` selalu ikut di output. Jangan menyembunyikannya.
- Dimensi yang tidak dapat dinilai ditandai demikian, **bukan** diberi nilai bawaan.

## Evidence

Setiap data point dibungkus sebagai evidence record dengan `metric`, `value`, `unit`, `geography`, `source`, `observed_at`, `retrieved_at`, `quality`, dan `limitations`. Angka tanpa provenance tidak boleh masuk report final sebagai fakta.

- Jangan meminta LLM mengisi data yang tidak tersedia. Field kosong tetap kosong dan menurunkan confidence.
- Category mapping versioned; jangan membandingkan hitungan kompetitor lintas versi taxonomy.
- Google Places tidak dipakai sebelum review lisensi selesai.

## Aturan OASIS

- Satu run memakai environment dan file trace **unik**. Jangan memakai path global, jangan menghapus file lama secara implisit seperti contoh quick-start.
- Output agent wajib schema-validated. Kegagalan berulang menjadikan run `partial`, bukan melempar exception ke pengguna.
- Persona tidak membawa memory lintas user atau lintas skenario.
- Simpan `model_id` persis, prompt version, cohort version, dan seed pada run manifest.
- Hard limit wajib ada: jumlah persona, round, concurrency, token, wall-clock, dan retry.
- Teks dari pengguna dan evidence diperlakukan sebagai **data ter-delimit**, tidak pernah digabung menjadi system prompt.

## State dan kegagalan

State machine analysis:

```text
queued -> collecting_evidence -> building_context -> simulating
  -> calculating_finance -> scoring -> composing_report
  -> validating_report -> completed | partial | failed | cancelled
```

- `partial` dipakai bila report deterministik tersedia tetapi komponen non-esensial gagal. **Jangan pernah mengembalikan `completed` untuk run parsial.**
- Progres berasal dari weighted stage completion nyata, bukan timer.
- Retry hanya untuk error transient. Error schema atau policy tidak diulang tanpa perubahan.
- Timeout terpisah per provider dan per LLM call.

## Keamanan dan privasi

- Password di-hash dengan Argon2id atau bcrypt berparameter yang ditinjau.
- Setiap query resource menyertakan scope owner/tenant **di repository layer**, bukan hanya di router. Jangan mengandalkan filter di UI.
- Access token berumur pendek; refresh token dirotasi dan dapat dicabut.
- Signed URL untuk export dan upload, berumur pendek.
- Validasi MIME lewat magic bytes, bukan extension atau header client.
- Strip EXIF bila tidak dibutuhkan.
- **Jangan kirim ke LLM:** email, nomor telepon, nama pelanggan, catatan bebas transaksi, teks struk mentah. Pseudonymize `user_id`, `business_id`, `analysis_id` sebelum inference eksternal.
- **Jangan log:** password, JWT, API key, prompt penuh, atau detail transaksi yang tidak diperlukan. Log metadata.
- Error ke client tidak memuat stack trace, prompt, atau respons provider mentah.

## API

- Prefix `/v1`. OpenAPI dari FastAPI adalah source of truth.
- Uang sebagai integer, timestamp ISO 8601 UTC, ID berupa UUID.
- POST yang dapat diulang menerima `Idempotency-Key`.
- Error memakai shape stabil dengan `code`, `message` berbahasa Indonesia, `fields`, `correlation_id`, dan `retryable`.
- Correlation ID mengalir dari API ke Celery, ke trace OASIS, hingga ke report.

Bentuk endpoint ada di `Docs/docs/06-api-contract.md`. Perubahan bentuk endpoint yang sudah dipakai frontend butuh ADR.

## Testing

- Engine finance dan scoring wajib punya golden test dengan fixture JSON. Fixture ini dibagikan dengan frontend.
- Test engine berjalan tanpa jaringan, tanpa database, tanpa LLM.
- Uji juga jalur gagal: provider timeout, schema invalid, marjin negatif, evidence kosong.
- Uji otorisasi lintas user untuk setiap resource.

```bash
pytest
ruff check .
ruff format --check .
mypy app
alembic upgrade head
```

Sebelum menyatakan task selesai, jalankan minimal `pytest`, `ruff check`, dan `mypy`. Laporkan hasil apa adanya.

## Yang tidak boleh dilakukan

- Memakai `float` untuk uang.
- Meminta LLM menghitung, menjumlahkan, atau memperkirakan angka yang masuk report.
- Menyimpan hasil OCR sebagai transaksi final tanpa konfirmasi pengguna.
- Menjadikan Redis system of record atau tempat menyimpan report final.
- Memodifikasi OASIS agar langsung memakai PostgreSQL tanpa spike yang membuktikan kebutuhannya.
- Menaikkan Python ke 3.12+.
- Memakai model LLM berlabel `-preview` pada jalur demo.
