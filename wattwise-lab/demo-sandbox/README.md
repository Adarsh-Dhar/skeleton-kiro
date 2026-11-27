# WATTWISE demo sandbox: SYNTHETIC DATA ONLY

Everything in this folder is fabricated, to exercise the WATTWISE workflow end to end: tariff entry, meter import,
dishwasher entry, bill reconciliation, an experiment and the audit. It runs against its **own** database (`sandbox.db` here),
never the real `data/trialforge.db`, and every input is labelled `unverified` / "fictional demo". The real-data sign-off
therefore **cannot pass** on it, by design.

| File | What it is |
|---|---|
| `01_sample_bill_DEMO.md` | Fictional October 2026 bill and slab tariff (447 kWh, Rs 2586.94) |
| `02_demo_meter_readings_30min.csv` | Fabricated 30-minute meter file (made by `make_demo_data.py`) |
| `make_demo_data.py` | Regenerates the CSV deterministically |
| `run_demo.py` | Runs the whole workflow on `sandbox.db` and writes `demo-report.md` / `demo-report.json` |

```
../.venv/bin/python make_demo_data.py
../.venv/bin/python run_demo.py          # add --serve to browse the sandbox on http://127.0.0.1:8092
```

Do not import any of these files into a real installation.
