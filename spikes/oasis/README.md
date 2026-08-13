# OASIS feasibility spike

Spike ini menguji kelayakan empat tipe agent SimuMarket AI tanpa menjadi
dependency aplikasi backend. Environment terpisah diperlukan karena
`camel-oasis==0.2.5` mengunci dependency pengembangan yang bentrok dengan
tooling backend utama.

Spike tidak menghasilkan skor atau angka finansial melalui LLM. Hasil finance
dibuat oleh kalkulator deterministik, lalu Finance Agent hanya mengkritik
asumsinya. Setiap run live memakai direktori dan database trace unik.

## Menjalankan

```powershell
uv sync --project spikes/oasis
uv run --project spikes/oasis python spikes/oasis/run.py probe
$env:GEMINI_API_KEY = "..."
uv run --project spikes/oasis python spikes/oasis/run.py live --runs 3
```

Model dapat diganti dengan `OASIS_MODEL_ID`. Nilai default mengikuti keputusan
teknis proyek, yaitu `gemini-3.1-flash-lite`.

Artifact live tersimpan di `spikes/oasis/artifacts/` dan sengaja diabaikan Git.
Jangan commit trace, respons model, atau API key. Angka benchmark baru boleh
ditulis ke dokumentasi setelah run live benar-benar selesai.

## Pemeriksaan lokal

```powershell
uv run --project spikes/oasis pytest spikes/oasis/tests
```

