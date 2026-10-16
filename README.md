# Production-Grade Contractor Lead Generation & Enrichment Engine

A high-precision, production-grade intelligence and enrichment platform for U.S. service contractors. The system combines state corporate filing ingestion (Florida Sunbiz) with an advanced asynchronous website crawler and multi-signal trade verification engine to discover, crawl, qualify, and segregate genuine contractors while strictly filtering out non-qualifying businesses, retail chains, and directories.

---

## 🌟 Key Features & Capabilities

1. **Christmas & Holiday Lighting Installation Classifier (`classify_christmas_lights.py`)**:
   - Crawls contractor websites and examines seasonal service pages, metadata, and JSON-LD schema.
   - Accurately qualifies genuine holiday lighting installers while preserving seasonal multi-trades (landscapers, window washers, and roofers who offer winter holiday lighting).
   - Strictly excludes event/DJ lighting, landscape-only lighting, general electricians, product retailers, and casual greetings.
   - Automatically segregates leads into 4 dedicated output CSV files.
2. **State Filing Acquisition & Sunbiz Ingestion**:
   - Streams official Florida Department of State (Sunbiz) corporate records directly from public SFTP/HTTPS servers without holding gigabytes in memory.
3. **Zero Commercial API Dependencies**:
   - Operates entirely on direct public state records, public search discovery, and native asynchronous web crawling—no paid API keys, commercial data vendors, or SaaS lock-in.
4. **Resilient Anti-Bot Website Discovery & Crawling**:
   - Uses modern browser fingerprinting (Chrome 131), proactive service endpoint probing, deep 2-tier subpage discovery, and fast-path retail domain exclusion.
5. **Deduplication & Data Integrity**:
   - 100% preservation of all original input columns, data formatting, and multi-line fields, with added diagnostic classification columns and evidence tracking.

---

## 🎄 Christmas & Holiday Lighting Lead Classification Engine

The Christmas lighting pipeline reads raw lead CSVs, crawls company websites, and programmatically determines genuine on-site installation service providers.

```mermaid
flowchart TD
    A[Raw Input CSV] -->|Preserve All 34+ Columns| B[URL & Domain Normalizer]
    B -->|Fast-Check Retail Domains| C{Known Retailer?}
    C -->|Yes: Home Depot, Walmart, etc.| D[Classify: Not Qualified]
    C -->|No| E[Async Crawler with Modern Headers]
    E -->|Probe /holiday-lighting, /christmas-light-installation| F[Multi-Page Content Aggregator]
    F -->|250+ Service Phrases & Regex Patterns| G{Holiday Service Verified?}
    G -->|Yes: Dedicated or Multi-Trade Seasonal| H[Classify: Qualified Leads.csv]
    G -->|No: Event/DJ, Landscape-only, Electrician| I[Classify: Not Qualified Leads.csv]
    G -->|No: Active Site with No Holiday Services| I
    G -->|Site Inaccessible / Timeout / 404| J[Classify: Unreachable Leads.csv]
    G -->|Minimal Content / Insufficient Info| K[Classify: Uncertain.csv]
    H & I & J & K --> L[Atomic Flush to Output CSVs + Consolidated All Leads.csv]
```

### Qualification Rules

| Target Niches (Qualified) | Strict Exclusions (Not Qualified) |
|---|---|
| **Holiday Lighting Installation Companies** | **Landscaping-only / Landscape lighting** (pathway, garden uplighting without holiday service) |
| **Christmas Lighting Installation Companies** | **General electrical contractors** (indoor fixtures, ceiling fans, breaker panels) |
| **Permanent Holiday Lighting Contractors** (Trimlight, Gemstone, Jellyfish, Everlights) | **Event, DJ, wedding, and stage lighting companies** |
| **Seasonal Multi-Trades** (Landscapers, window cleaners, roofers with verified winter holiday lighting) | **Retailers, e-commerce stores, and wholesale lighting suppliers** |
| | **Casual holiday greetings** ("Closed for Christmas", "Holiday hours") with no lighting services |

### Running the Christmas Lighting Classifier

```bash
# Activate virtual environment
.\.venv\Scripts\activate

# Run the classifier using default input (auto-detects the raw CSV in christmas light installation/)
python classify_christmas_lights.py

# Optional: Run with custom concurrency and timeout
python classify_christmas_lights.py --concurrency 16 --timeout 15.0

# Optional: Run a test batch on the first 50 leads
python classify_christmas_lights.py --limit 50

# Optional: Disable resume or customize output folder
python classify_christmas_lights.py --output "christmas light installation/output" --no-resume
```

### Output Files Generated

Outputs are saved directly inside `christmas light installation/output/`:

1. **`Qualified Leads.csv`**: Verified holiday and Christmas lighting installation contractors (including seasonal multi-trades).
2. **`Not Qualified Leads.csv`**: Excluded businesses (retailers, electricians, event lighting, landscape-only).
3. **`Unreachable Leads.csv`**: Websites that could not be accessed due to connection errors, DNS failure, or persistent timeouts.
4. **`Uncertain.csv`**: Leads where available website content is too minimal to make a confident decision.
5. **`All Leads.csv`**: Consolidated master file with diagnostic columns:
   - `Does this company provide Christmas light installation?` (`Yes`, `No`, `Uncertain`, `Unreachable`)
   - `Website status` (`OK`, `Connection Error`, `Timeout`, `Bot Challenge`, etc.)
   - `Classification reasoning` (Detailed human-readable audit justification)
   - `Website access error` (Exact network exception if unreachable)
   - `Evidence` (Exact matched phrases and regex patterns)

---

## 🏗️ State Filing Ingestion Engine (Florida Sunbiz)

The Florida Sunbiz pipeline ingests official state corporate filings, normalizes business names, classifies primary trade categories, discovers candidate websites, and extracts verified domain-aligned business emails.

```bash
# Run Sunbiz pipeline via the CLI
python -m contractor_scraper.cli.app run --year 2025 --category Roofing --limit 1000
```

---

## 🚀 Installation & Setup

### Prerequisites
- **Python**: Version `3.11`, `3.12`, `3.13`, or `3.14` (64-bit).
- **Git**: Installed on your system.

### 1. Clone & Set Up Virtual Environment

```bash
git clone <REPOSITORY_URL>
cd contractors_finding_scrapper

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 🧪 Testing & Verification

The project includes a comprehensive automated test suite with deterministic mocks covering web crawling, trade classification, subpage extraction, email verification, and CSV enrichment.

```bash
# Run complete test suite (115 tests)
pytest tests/ -v

# Run Christmas lighting enrichment tests specifically
pytest tests/test_christmas_light_enricher_improvements.py -v
```

---

## 📁 Project Directory Structure

```text
contractors_finding_scrapper/
├── classify_christmas_lights.py      # Christmas lighting CLI runner
├── enrich_pest_control.py             # Pest control lead enrichment runner
├── pyproject.toml                     # Project packaging and tool configuration
├── requirements.txt                   # Production dependencies
├── .gitignore                         # Configured ignore patterns
├── christmas light installation/      # Working folder (ignored in git)
│   ├── Christmas Lights Installation...csv  # Raw input leads (5,300+ records)
│   └── output/                        # Categorized CSV output folder
│       ├── Qualified Leads.csv
│       ├── Not Qualified Leads.csv
│       ├── Unreachable Leads.csv
│       ├── Uncertain.csv
│       └── All Leads.csv
├── src/
│   └── contractor_scraper/
│       ├── acquisition/               # Sunbiz SFTP/HTTPS data downloader
│       ├── classification/            # Christmas light & trade classifiers
│       │   ├── christmas_light_classifier.py
│       │   └── classifier.py
│       ├── discovery/                 # Website discovery & crawler
│       │   ├── crawler.py             # Resilient async browser crawler
│       │   └── website_finder.py
│       ├── extraction/                # Business email and contact extraction
│       ├── orchestration/             # Lead enrichment pipeline runners
│       │   ├── christmas_light_enricher.py
│       │   └── lead_engine.py
│       └── storage/                   # SQLite WAL lead database & checkpoints
└── tests/                             # Comprehensive test suite (115 tests)
```

---

## 📄 License

Proprietary & Confidential. Developed for production contractor lead generation and qualification.
