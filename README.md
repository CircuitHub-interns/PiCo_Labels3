# PiCo_Labels

Generator for circular pick-and-place nozzle labels with Data Matrix barcodes. The script takes an SVG label-sheet template, fills it with a grid of round labels — each carrying a curved part-name text and a scannable Data Matrix code — and writes print-ready SVG pages plus JSON manifests that track every tool/slot ever generated.

The main entry point is **`Goat/PiCo_LabelsV21.py`**.

---

## Requirements

### 1. Python 3.9+ (64-bit)

The 64-bit requirement matters on Windows: the `pylibdmtx` wheel ships a 64-bit native DLL that will not load under 32-bit Python.

### 2. Python packages

```
pip install treepoem pylibdmtx Pillow
```

| Package | What it's used for |
|---|---|
| **treepoem** | Generates the Data Matrix barcode images (wraps BWIPP via Ghostscript) |
| **Pillow** (PIL) | Image handling — converting barcodes to 1-bit PNG, adding white quiet-zone borders for decode tests |
| **pylibdmtx** | *Decodes* Data Matrix images — only needed if barcode verification is enabled (it is off by default, see [Verification](#barcodeverifier)) |

### 3. Ghostscript (system install, not pip)

`treepoem` renders barcodes through Ghostscript, which is a standalone program:

- **Windows:** download the 64-bit installer from https://ghostscript.com/releases/gsdnld.html and install it. The installer normally puts `gswin64c` on the PATH; if treepoem complains it can't find Ghostscript, add `C:\Program Files\gs\gs<version>\bin` to PATH manually.
- **macOS:** `brew install ghostscript` (and `brew install libdmtx` if you want verification — the script auto-adds `/opt/homebrew/lib` to `DYLD_LIBRARY_PATH` for IDE runs).
- **Linux:** `sudo apt install ghostscript libdmtx0b` (or your distro's equivalent).

### 4. Font

Labels are typeset in **Roboto Mono**. Install it on the machine that renders/prints the SVGs, otherwise the viewer will substitute a different font and the curved text spacing will be off.

---

## Quick start

```
git clone https://github.com/CircuitHub-interns/PiCo_Labels3.git
cd PiCo_Labels3
uv sync                                   # creates .venv with everything from pyproject.toml
uv run python Goat/PiCo_LabelsV21.py      # SVG sheet generator (interactive)
uv run python "Goat/ZPL Goat/ZPL_Script.py" CH0204 --png preview.png   # single label as ZPL
```

No uv? `pip install treepoem pylibdmtx Pillow setuptools` works too (`setuptools` is needed on Python 3.12+ because pylibdmtx still imports `distutils`).

The script is fully interactive — it walks you through mode, gantry, and part selection (see [Interactive flow](#interactive-flow)).

> **Note:** all paths resolve relative to the script's own folder (`BASE_DIR = Path(__file__).resolve().parent`), so templates, counters, and outputs are always found next to the script in `Goat/` regardless of your current working directory.

---

## Repository layout

```
├── Goat/
│   ├── PiCo_LabelsV21.py          # Main script (this README documents it)
│   ├── onlygodknows.svg           # PRODUCTION page template
│   ├── TURKEY.svg                 # TEST page template
│   ├── allowed_parts.txt          # Whitelist of valid part names (script-managed)
│   ├── label_test_manifest.json   # Manifest from the latest TEST run
│   ├── verification_report.json   # Barcode verification report from the latest run
│   ├── label_test*.svg            # Output of test runs
│   └── SWAP Outputs/              # All PRODUCTION output
│       ├── global_counter.txt     # Last N-number used (shared across all gantries)
│       ├── global_manifest.json   # Every tool ever generated, grouped by gantry
│       └── G1/ ... G5/            # One folder per gantry
│           ├── G#_V1.svg, ...     # Versioned print pages
│           └── G#_manifest.json   # Full run history + tool list for that gantry
├── allowed_parts.txt              # (root copy — the script reads Goat/allowed_parts.txt)
└── README.md
```

---

## How it works

### The label sheet

Each generated SVG is the chosen template with a **9 × 13 grid of 117 circular labels** stamped onto it. Each label consists of three SVG elements:

1. **An invisible circular path** (radius 13 units) that the text follows.
2. **A `<textPath>`** with the label text (e.g. `P056-0042` or `REFERENCE-7`) curved around the circle in Roboto Mono, font size 7. Letter spacing and rotation angle are tuned per part-name length so any name sits centered on its arc (see `_style_for_part`).
3. **A Data Matrix image** (8.6 × 8.6 units), embedded as a base64 PNG and rotated 180°, positioned inside the circle. The barcode encodes only the **slot ID** (`N0001`, `N0002`, …) — compact and machine-readable — while the human-readable part name lives in the curved text.

Grid geometry (spacing, margins, circle radius) is hard-coded in `_label_geometry()` and has been hand-tuned against the physical label stock — the inline comments record the calibration history. Change those numbers only when re-calibrating against a printed test sheet.

### Two modes

| | **Test mode** | **Production mode** |
|---|---|---|
| Text color | White | Black |
| Labels generated | One per unique part entered | Full 117-slot page (parts cycle to fill it) |
| Slot numbering | Restarts at N0001 every run | Continues from the persistent global counter |
| Output file | `Goat/label_test.svg` (or `label_test_1.svg`, `_2.svg`… if multi-page) | `Goat/SWAP Outputs/<gantry>/<gantry>_V<n>.svg` — version auto-increments, old files are never overwritten |
| Manifest | `Goat/label_test_manifest.json` (overwritten each run) | Appended to `<gantry>_manifest.json` and `global_manifest.json` |
| Template | `TURKEY.svg` | `onlygodknows.svg` |

### Interactive flow

Running the script asks, in order:

1. **Mode** — `1` test / `2` production (default: production).
2. **Gantry** (production only) — one of `G1`–`G5`. Determines the output folder and which manifest gets updated.
3. **Parts** — enter part names one at a time; type `REFERENCE` for a reference label, `done` to finish.
   - Each entry is validated against `allowed_parts.txt`. Unknown parts prompt "add to allowed parts?" — accepting appends them to the file permanently.
   - Each entry is assigned the next slot ID (`N####`). In production the numbering continues across runs and across gantries via `SWAP Outputs/global_counter.txt`, so every physical tool ever labeled has a globally unique N-number.
4. The page(s) generate, manifests and the verification report are written, and a final menu offers to open the first SVG, the output folder, or the manifest.

In production mode, the entered parts are **cycled** to fill all 117 slots — entering 3 parts produces a sheet with 39 labels of each. Each entered part gets exactly one slot ID, and every printed copy of that part carries the same ID, so a sheet gives you many identical physical labels per tool. The manifest records one tool record per entered part, not per printed label.

### Persistent state

These files survive between runs — treat them as data, not scratch:

- **`SWAP Outputs/global_counter.txt`** — the last N-number issued. The next production run starts at N+1.
- **`SWAP Outputs/<gantry>/<gantry>_manifest.json`** — append-only history: every run (with timestamps, files, tools) plus a deduplicated cumulative tool list for that gantry.
- **`SWAP Outputs/global_manifest.json`** — the master registry: every tool number ever issued, grouped by gantry. This is the file to consult to answer "what is N0037 and which gantry does it belong to?"
- **`allowed_parts.txt`** — the part whitelist. One part per line, `#` comments allowed, auto-uppercased and deduplicated on load. If the file is missing it is regenerated from the defaults hard-coded in the script.

---

## Code tour (`PiCo_LabelsV21.py`)

### Module constants (lines 16–41)

`GRID_COLUMNS`/`GRID_ROWS`/`MAX_LABELS_PER_PAGE` define the 9×13=117 sheet. `TEST_TEMPLATE_FILE`/`PRODUCTION_TEMPLATE_FILE` name the two SVG templates. `DEFAULT_ALLOWED_PARTS` seeds `allowed_parts.txt` on first run. `VISUAL_TEMPLATE_COMPARISON_MODE` is a debug toggle that makes production labels white so they can be visually overlaid on the test template.

### `PartCatalog`

Owns `allowed_parts.txt`. Loads it (normalizing to uppercase, skipping blanks/comments, deduplicating), recreates it from defaults if missing or empty, and offers `contains()`/`add()`. Every `add()` writes through to disk immediately.

### `DataMatrixRenderer`

Wraps `treepoem.generate_barcode(barcode_type="datamatrix", ...)`. `render()` produces both a PIL image (for verification) and a base64 1-bit PNG string (for embedding in the SVG). `render_many()` renders a whole page's barcodes in parallel with a `ThreadPoolExecutor` — worker count defaults to `min(8, cpu_count)` and can be overridden with the **`ARIA_RENDER_WORKERS`** environment variable. The class exists so the barcode backend could be swapped without touching the page-building code.

### `BarcodeVerifier`

Optional quality gate that decodes every generated barcode with `pylibdmtx` and confirms the payload round-trips.

- **Currently disabled by default** — `_configure_mode()` constructs it with `enabled=False`, so runs report `verification_result: "skipped"`. To turn it on, change `BarcodeVerifier(enabled=False, ...)` to `enabled=True` in `_configure_mode()` (around line 528).
- When enabled: `run_health_check()` first proves the decoder works by generating and decoding a known payload; then `verify()` is called for every label, trying a raw decode and retrying with an 8-pixel white border (quiet zone) if the raw image won't read.
- `write_report()` always runs (enabled or not) and produces **`verification_report.json`**: pass/fail/skip counts, failure reasons, up to 25 failure samples with full context (which page, which position, which file), and the complete run context including a copy of the manifest.

### `Label_Gen` — the orchestrator

Everything else lives here. The important methods, in the order `run()` uses them:

| Method | What it does |
|---|---|
| `_configure_mode()` | Asks test vs production, sets text color |
| `_select_gantry()` | Production only — asks G1–G5 |
| `_read_n_counter()` / `_save_n_counter()` | Load/persist the global N-counter in `SWAP Outputs/global_counter.txt` |
| `_collect_swap_parts()` | Interactive part entry; builds `swap_parts` (one record per entered part, each with a unique `N####` slot ID) and `slot_sequence` (the 117-slot page layout, cycling the entered parts) |
| `_style_for_part(part)` | Returns `(letter_spacing, rotation_angle)` tuned to the part-name length (4–7+ chars) so the curved text is centered; `REFERENCE` has its own style |
| `_label_geometry()` | The calibrated grid: start coordinates, spacing, circle radius |
| `_build_page_jobs()` | Turns the slot sequence into per-label "jobs": position, text, barcode payload, style, and verification context |
| `_generate_page()` | Deep-copies the template SVG root, renders all barcodes (in parallel), then appends the three SVG elements per label and writes the page file |
| `generate_pages()` | Splits the request into pages of ≤117 and names each output file — versioned in production via `_next_production_output_version()`, which scans the folder for the highest existing `_V<n>` and adds 1 |
| `_build_database_manifest()` / `_write_database_manifest()` | Build the run manifest and merge it into the per-gantry and global manifests (production) or write `label_test_manifest.json` (test) |
| `_post_generation_action()` | Final menu: open SVG / folder / manifest / skip |

### Manifest schema (v2)

Per-run manifest (what gets appended to a gantry's `runs` list):

```json
{
  "schema_version": 2,
  "event_type": "pico_labels.generated",
  "created_at_utc": "...",
  "mode": "production",
  "gantry": "G2",
  "tools": [
    { "tool_number": "N0007", "tool_name": "P056", "gantry": "G2",
      "serial_id": 7, "padded_serial": "0007" }
  ],
  "files": { "generated_svgs": ["..."], "template_file": "...", "verification_report": "..." }
}
```

The gantry manifest wraps runs in `{ "runs": [...], "tools": [...] }` (tools deduplicated by `tool_number`); the global manifest is `{ "gantries": { "G1": [tools...], ... } }`.

---

## Common tasks

**Print a production sheet for gantry G3:** run the script → `2` → `G3` → enter parts → `done`. Print the newly created `SWAP Outputs/G3/G3_V<n>.svg`.

**Add a new part permanently:** type it during part entry and accept the "add to allowed parts?" prompt — or add a line to `Goat/allowed_parts.txt` by hand.

**Check what a scanned barcode means:** the Data Matrix encodes the slot ID (e.g. `N0042`). Look it up in `SWAP Outputs/global_manifest.json` to find its part name and gantry.

**Turn barcode verification on:** set `enabled=True` in the `BarcodeVerifier(...)` call in `_configure_mode()`. Requires `pylibdmtx` to import cleanly.

**Speed up / slow down rendering:** set `ARIA_RENDER_WORKERS=<n>` in the environment before running.

**Reset slot numbering (careful):** delete `SWAP Outputs/global_counter.txt`. Only do this if you also retire the manifests — otherwise new labels will reuse N-numbers that already exist on physical tools.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `treepoem` error about `gs` / `gswin64c` not found | Install Ghostscript and make sure its `bin` folder is on PATH |
| `pylibdmtx` fails to import on Windows | You're on 32-bit Python — install 64-bit Python |
| `FileNotFoundError: Template file not found` | `TURKEY.svg` / `onlygodknows.svg` must sit next to the script in `Goat/` |
| Curved text looks mis-spaced when printed | Install the Roboto Mono font on the rendering machine |
| Verification always reports `skipped` | That's the default — verification is disabled in code (see above) |
