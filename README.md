# Bundestag Parliamentary Discourse Dataset — Pipeline

Source code for the Bundestag Plenarprotokolle corpus: 1,033,723 speeches from
4,620 plenary sessions (WP01–WP21, 1949–2026), each attributed to a speaker with
name, role, party, gender, and full text.

**Dataset (Zenodo):** https://doi.org/10.5281/zenodo.21258818  
**Authors:** Adama Njie, Ali E. Torkayesh, Sandra Venghaus (RWTH Aachen University)

---

## Pipeline overview

| Script | Input | Output |
|---|---|---|
| `b_00_text_extractor.py` | Raw PDFs (`data/raw/`) | `*_metadata.json`, `*.txt` |
| `b_01_text_cleaning.py` | `*.txt` | `*_cleaned.txt` |
| `b_02_speaker_identifier.py` | `*_cleaned.txt` | `*_speakers.json` |
| `e_01_gender.py` | `*_speakers.json` | `gender_lookup.csv` |
| `f_01_build_dataset.py` | `*_speakers.json` + gender CSV | `speeches.parquet`, `sessions.parquet`, `speakers.parquet` |
| `g_01_corpus_statistics.py` | Parquet tables | Figures and CSV tables |
| `c_01_validate_corpus.py` | `*_speakers.json` | `validation_report.txt` |
| `h_01_quality_audit.py` | `speeches.parquet` | Quality audit CSV |

Run a single Wahlperiode end-to-end:

```bash
python src/run_pipeline.py --wp 20
```

Run all WPs (HPC recommended):

```bash
python src/run_pipeline.py \
    --input-dir  /path/to/raw \
    --output-dir /path/to/preprocessed \
    --parties-csv docs/political-parties-past-75-years.csv
```

Skip stages already completed:

```bash
python src/run_pipeline.py --wp 20 --skip-extract --skip-clean
```

## Setup

```bash
pip install -r requirements.txt
```

Raw PDFs are downloaded from the [Bundestag open data portal](https://www.bundestag.de/dokumente/protokolle/plenarprotokolle).
Place them in `data/raw/` using the Bundestag filename convention: `WWSSS.pdf`
(e.g. `20146.pdf` → WP 20, Session 146).

`docs/MdB-Stammdaten.xml` is required for gender annotation (e_01). Download from
bundestag.de/MdB-Stammdaten-data.zip and place in `docs/`.

## Reference files (docs/)

| File | Purpose |
|---|---|
| `political-parties-past-75-years.csv` | Party abbreviations for parser |
| `bundestag_election_results.csv` | Seat counts for g_01 normalisation |
| `excluded_false_speakers.csv` | Names excluded as false attributions in f_01 |
| `manual_gender_overrides.csv` | Hand-corrected gender assignments |
| `reqs.txt` | Speaker attribution rules (Pattern A/B/C/D specification) |

## Citation

```bibtex
@dataset{njie2026bundestag,
  author    = {Njie, Adama and Torkayesh, Ali E. and Venghaus, Sandra},
  title     = {A Parliamentary Discourse Dataset from the German Bundestag},
  year      = {2026},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.21258818}
}
```
