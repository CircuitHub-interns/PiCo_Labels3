import base64
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

import treepoem
from PIL import ImageOps

GRID_COLUMNS                    = 9
GRID_ROWS                       = 13
MAX_LABELS_PER_PAGE             = 117
DEFAULT_STARTING_PART           = "CH0204"
ALLOWED_PARTS_FILE              = "allowed_parts.txt"
VERIFICATION_REPORT_FILE        = "verification_report.json"
MANIFEST_SCHEMA_VERSION         = 2
TEST_TEMPLATE_FILE              = "TURKEY.svg" # V14 now testing turkey to see if they are the same
PRODUCTION_TEMPLATE_FILE        = "onlygodknows.svg" 
VISUAL_TEMPLATE_COMPARISON_MODE = False # Toggle On and Off for circle position testing
DEFAULT_ALLOWED_PARTS = [
    "P056",
    "P057",
    "P054",
    "P055",
    "P017",
    "P018",
    "P019",
    "P063",
    "CH0406",
    "CH0204",
    "CH01503",
]

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
DEFAULT_RENDER_WORKERS = min(8, max(2, os.cpu_count() or 4))
BASE_DIR = Path(__file__).resolve().parent


def project_path(path):
    path = Path(path)
    return path if path.is_absolute() else BASE_DIR / path


class PartCatalog:
    def __init__(self, file_path=ALLOWED_PARTS_FILE, defaults=None):
        self.file_path = project_path(file_path)
        self.defaults = list(defaults or DEFAULT_ALLOWED_PARTS)
        self.parts = []
        self.load()

    @staticmethod
    def _dedupe_keep_order(items):
        seen = set()
        deduped = []
        for item in items:
            if item not in seen:
                deduped.append(item)
                seen.add(item)
        return deduped

    def load(self):
        if not os.path.exists(self.file_path):
            self.parts = self._dedupe_keep_order([p.upper() for p in self.defaults])
            self.save()
            return

        loaded = []
        with open(self.file_path, "r", encoding="utf-8") as f:
            for line in f:
                cleaned = line.strip().upper()
                if not cleaned or cleaned.startswith("#"):
                    continue
                loaded.append(cleaned)

        if not loaded:
            loaded = [p.upper() for p in self.defaults]

        self.parts = self._dedupe_keep_order(loaded)
        self.save()

    def save(self):
        with open(self.file_path, "w", encoding="utf-8") as f:
            for part in self.parts:
                f.write(f"{part}\n")

    def contains(self, part):
        return part.upper() in self.parts

    def add(self, part):
        normalized = part.upper()
        if normalized in self.parts:
            return False
        self.parts.append(normalized)
        self.save()
        return True


class BarcodeVerifier:
    """Optional readability checks for generated Data Matrix images."""

    def __init__(self, enabled=False, verification_scope="all"):
        self.enabled = enabled
        self.verification_scope = verification_scope
        self.decode_fn = None
        self.init_error = None
        self.health_check = {
            "status": "not_run",
            "reason": "health_check_not_started",
            "expected_payload": None,
            "decoded_payload": None,
            "decode_strategy": None,
        }
        self.summary = {
            "labels_seen": 0,
            "labels_checked": 0,
            "pass": 0,
            "fail": 0,
            "skipped": 0,
        }
        self.failure_samples = []
        self.max_failure_samples = 25
        self.failure_reason_counts = Counter()
        self.failure_signature_counts = Counter()
        if self.enabled:
            self._initialize()

    def _initialize(self):
        self._ensure_macos_library_paths()
        try:
            from pylibdmtx.pylibdmtx import decode as decode_fn

            self.decode_fn = decode_fn
        except Exception as exc:
            self.init_error = exc

    @staticmethod
    def _ensure_macos_library_paths():
        # Helps IDE runs where DYLD_LIBRARY_PATH is not set.
        candidate_dirs = ["/opt/homebrew/lib", "/usr/local/lib"]
        valid_dirs = []
        for directory in candidate_dirs:
            if os.path.exists(os.path.join(directory, "libdmtx.dylib")):
                valid_dirs.append(directory)

        if not valid_dirs:
            return

        current = os.environ.get("DYLD_LIBRARY_PATH", "")
        current_dirs = [d for d in current.split(":") if d]
        changed = False
        for directory in valid_dirs:
            if directory not in current_dirs:
                current_dirs.insert(0, directory)
                changed = True

        if changed:
            os.environ["DYLD_LIBRARY_PATH"] = ":".join(current_dirs)

    def _record_failure_sample(self, result):
        if len(self.failure_samples) < self.max_failure_samples:
            self.failure_samples.append(result)

    def _decode_payload(self, barcode_image):
        decode_attempts = [
            ("raw", barcode_image),
            ("white_border_8px", ImageOps.expand(barcode_image, border=8, fill="white")),
        ]
        last_exception = None

        for strategy_name, candidate in decode_attempts:
            try:
                # PIL image decode path. RGB is the most reliable input format here.
                read_result = self.decode_fn(candidate.convert("RGB"))
                if not read_result:
                    continue
                decoded_data = read_result[0].data.decode(errors="replace")
                return decoded_data, None, strategy_name
            except Exception as exc:
                last_exception = exc

        if last_exception is not None:
            return None, f"decode_exception: {last_exception}", None
        return None, "not_readable", None

    def run_health_check(self):
        if not self.enabled:
            self.health_check = {
                "status": "skipped",
                "reason": "verification_disabled",
                "expected_payload": None,
                "decoded_payload": None,
                "decode_strategy": None,
            }
            print("[VERIFY] Health check skipped (verification disabled).")
            return self.health_check

        if self.decode_fn is None:
            self.health_check = {
                "status": "failed",
                "reason": f"decoder_unavailable: {self.init_error}",
                "expected_payload": None,
                "decoded_payload": None,
                "decode_strategy": None,
            }
            print("[VERIFY] Health check failed: decoder unavailable.")
            return self.health_check

        expected_payload = "HEALTHCHECK-ARIA-0001"
        try:
            health_image = treepoem.generate_barcode(
                barcode_type="datamatrix",
                data=expected_payload,
            )
        except Exception as exc:
            self.health_check = {
                "status": "failed",
                "reason": f"health_image_generation_failed: {exc}",
                "expected_payload": expected_payload,
                "decoded_payload": None,
                "decode_strategy": None,
            }
            print("[VERIFY] Health check failed: barcode generation issue.")
            return self.health_check

        decoded_payload, decode_error, decode_strategy = self._decode_payload(health_image)
        if decode_error:
            self.health_check = {
                "status": "failed",
                "reason": decode_error,
                "expected_payload": expected_payload,
                "decoded_payload": decoded_payload,
                "decode_strategy": decode_strategy,
            }
            print(f"[VERIFY] Health check failed: {decode_error}.")
            return self.health_check

        if decoded_payload != expected_payload:
            self.health_check = {
                "status": "failed",
                "reason": "payload_mismatch",
                "expected_payload": expected_payload,
                "decoded_payload": decoded_payload,
                "decode_strategy": decode_strategy,
            }
            print("[VERIFY] Health check failed: payload mismatch.")
            return self.health_check

        self.health_check = {
            "status": "passed",
            "reason": "decoder_ready",
            "expected_payload": expected_payload,
            "decoded_payload": decoded_payload,
            "decode_strategy": decode_strategy,
        }
        print("[VERIFY] Health check passed.")
        return self.health_check

    def verify(self, barcode_image, expected_data, context=None):
        context = context or {}
        result = {
            "expected_data": expected_data,
            "decoded_data": None,
            "decode_strategy": None,
            "status": "unknown",
            "reason": None,
            "context": context,
        }
        self.summary["labels_seen"] += 1

        if not self.enabled:
            result["status"] = "skipped"
            result["reason"] = "verification_disabled"
            self.summary["skipped"] += 1
            return result

        if (
            self.verification_scope == "first_only"
            and self.summary["labels_checked"] >= 1
        ):
            result["status"] = "skipped"
            result["reason"] = "verification_scope_first_only"
            self.summary["skipped"] += 1
            return result

        if self.decode_fn is None:
            result["status"] = "skipped"
            result["reason"] = f"decoder_unavailable: {self.init_error}"
            self.summary["skipped"] += 1
            return result

        self.summary["labels_checked"] += 1
        decoded_data, decode_error, decode_strategy = self._decode_payload(barcode_image)
        result["decoded_data"] = decoded_data
        result["decode_strategy"] = decode_strategy
        if decode_error:
            result["status"] = "fail"
            result["reason"] = decode_error
            self.summary["fail"] += 1
            self.failure_reason_counts[decode_error] += 1
            signature = f"{decode_error}|decoded={decoded_data}"
            self.failure_signature_counts[signature] += 1
            self._record_failure_sample(result)
            print(f"[VERIFY][FAIL] {expected_data} ({decode_error})")
            return result

        if decoded_data != expected_data:
            result["status"] = "fail"
            result["reason"] = "payload_mismatch"
            self.summary["fail"] += 1
            self.failure_reason_counts["payload_mismatch"] += 1
            signature = f"payload_mismatch|decoded={decoded_data}"
            self.failure_signature_counts[signature] += 1
            self._record_failure_sample(result)
            print(
                "[VERIFY][FAIL] "
                f"{expected_data} (decoded {decoded_data}, payload mismatch)"
            )
            return result

        result["status"] = "pass"
        result["reason"] = "payload_match"
        self.summary["pass"] += 1
        print(f"[VERIFY][PASS] {expected_data}")
        return result

    def _build_verification_result(self):
        if not self.enabled:
            return "skipped"
        if self.health_check["status"] != "passed":
            return "fail"
        if self.summary["labels_checked"] == 0:
            return "fail"
        if self.summary["fail"] > 0:
            return "fail"
        return "pass"

    def _build_repeated_failure_signatures(self):
        repeated = []
        for signature, count in self.failure_signature_counts.most_common():
            if count > 1:
                repeated.append(
                    {
                        "signature": signature,
                        "count": count,
                    }
                )
        return repeated

    def write_report(self, report_path, run_context):
        report_path = project_path(report_path)
        verification_result = self._build_verification_result()
        report = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "verification_enabled": self.enabled,
            "verification_scope": self.verification_scope,
            "verification_result": verification_result,
            "health_check": self.health_check,
            "summary": self.summary,
            "failure_reason_counts": dict(self.failure_reason_counts),
            "repeated_failure_signatures": self._build_repeated_failure_signatures(),
            "failure_samples": self.failure_samples,
            "failure_samples_truncated": self.summary["fail"] > len(self.failure_samples),
            "run_context": run_context,
        }
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        return report


class DataMatrixRenderer:
    """Encapsulates Data Matrix rendering so the backend can be swapped later."""

    def __init__(self, barcode_type="datamatrix", worker_count=DEFAULT_RENDER_WORKERS):
        self.barcode_type = barcode_type
        self.worker_count = max(1, worker_count)

    @staticmethod
    def from_environment(barcode_type="datamatrix"):
        raw_value = os.environ.get("ARIA_RENDER_WORKERS", "").strip()
        if raw_value:
            try:
                return DataMatrixRenderer(
                    barcode_type=barcode_type,
                    worker_count=max(1, int(raw_value)),
                )
            except ValueError:
                pass
        return DataMatrixRenderer(barcode_type=barcode_type)

    def render(self, barcode_data):
        barcode_image = treepoem.generate_barcode(
            barcode_type=self.barcode_type,
            data=barcode_data,
        )
        buffered = BytesIO()
        barcode_image.convert("1").save(buffered, format="PNG")
        return barcode_image, base64.b64encode(buffered.getvalue()).decode()

    def render_many(self, barcode_jobs):
        if not barcode_jobs:
            return []

        if self.worker_count == 1 or len(barcode_jobs) == 1:
            return [self.render(job["barcode_data"]) for job in barcode_jobs]

        with ThreadPoolExecutor(max_workers=min(self.worker_count, len(barcode_jobs))) as executor:
            return list(
                executor.map(
                    lambda job: self.render(job["barcode_data"]),
                    barcode_jobs,
                )
            )


class Label_Gen:
    def __init__(self, starting_part=DEFAULT_STARTING_PART):
        self.default_part = starting_part.upper()
        self.template_path = project_path(TEST_TEMPLATE_FILE)

        self.catalog = PartCatalog()

        self.test_mode = True
        self.color = "white"
        self.labels_requested = 5

        self.aria_label_part = self.default_part
        self.aria_label_number = 0
        self.letter_spacing, self.rotation_angle = self._style_for_part(self.aria_label_part)
        self.counter_file = self._counter_file_for_part(self.aria_label_part)
        self.renderer = DataMatrixRenderer.from_environment()
        self._template_root = None

        # Off by default while focusing on generation workflow.
        self.verifier = BarcodeVerifier(enabled=False, verification_scope="all")

        self.swap_parts = []    # [{"part": str, "slot_id": str}, ...]
        self.slot_sequence = [] # 117-element list cycling swap_parts
        self.gantry = None      # set only in production mode

    def _load_template_root(self):
        template_file = TEST_TEMPLATE_FILE if self.test_mode else PRODUCTION_TEMPLATE_FILE
        self.template_path = project_path(template_file)
        if not self.template_path.exists():
            raise FileNotFoundError(f"Template file not found: {self.template_path}")
        ET.register_namespace("", SVG_NAMESPACE)
        return ET.parse(self.template_path).getroot()

    @staticmethod
    def _prompt_input(prompt):
        return input(f"{prompt}\n> ")

    @staticmethod
    def _style_for_part(part):
        if part == "REFERENCE":
            return .8, 225
        part_len = len(part)
        # Returns (letter_spacing, rotation_angle).
        # Angles are original + 180° — identifiers appear on the opposite arc of the circle.
        if part_len == 4:
            return 2.8, 239 # 218 is great but a little less is my attempt 230 previous goat but now we got rid of a letter so 245, also previous goat was 2.3 now 2.6
        if part_len == 5:
            return 1.6, 212
        if part_len == 6:
            return 1.4, 234
        if part_len == 7:
            return 1.25, 204
        return 0.0, 198

    @staticmethod
    def _ask_yes_no(prompt, default="Y"):
        default = default.upper()
        hint = "[Y/n]" if default == "Y" else "[y/N]"
        while True:
            raw = Label_Gen._prompt_input(f"{prompt} {hint}:").strip().upper()
            if not raw:
                return default == "Y"
            if raw in {"Y", "YES"}:
                return True
            if raw in {"N", "NO"}:
                return False
            print("Please enter Y or N.")

    @staticmethod
    def _ask_positive_int(prompt, default=None):
        while True:
            if default is None:
                raw = Label_Gen._prompt_input(f"{prompt}:").strip()
            else:
                raw = Label_Gen._prompt_input(f"{prompt} [{default}]:").strip()
                if raw == "":
                    return default

            try:
                value = int(raw)
                if value <= 0:
                    print("Please enter a number greater than 0.")
                    continue
                return value
            except ValueError:
                print("Please enter a valid whole number.")

    def _configure_mode(self):
        print("\nWhat would you like to create?")
        print("1) Test labels (white text)")
        if VISUAL_TEMPLATE_COMPARISON_MODE:
            print("2) Production labels (white text for template comparison)")
        else:
            print("2) Production labels (black text)")

        while True:
            mode = self._prompt_input("Choose 1 or 2 [2]:").strip()
            if mode in {"1"}:
                self.test_mode = True
                self.color = "white"
                break
            if mode in {"", "2"}:
                self.test_mode = False
                self.color = "white" if VISUAL_TEMPLATE_COMPARISON_MODE else "black"
                break
            print("Please enter 1 or 2.")

        self.verifier = BarcodeVerifier(enabled=False, verification_scope="all")
        print("[VERIFY] Scope: disabled.")

    def _select_gantry(self):
        valid = {"G1", "G2", "G3", "G4", "G5", "PENDING"}
        while True:
            raw = self._prompt_input(
                "What gantry is this for? (G1, G2, G3, G4, G5, or PENDING):"
            ).strip().upper()
            if raw in valid:
                self.gantry = raw
                print(f"Gantry: {self.gantry}")
                return
            print(f"Please enter one of: {', '.join(sorted(valid))}.")

    def _n_counter_file(self):
        return project_path("SWAP Outputs") / "global_counter.txt"

    def _read_n_counter(self):
        """Return the next N counter (one-based) to use.

        This implementation prefers the persisted global_counter.txt value but also
        scans all JSON manifests under SWAP Outputs for existing N numbers and
        ensures the next returned N is strictly greater than any seen. This avoids
        regressing the counter when manifests have been edited or imported.
        """
        counter_file = self._n_counter_file()
        counter_file.parent.mkdir(parents=True, exist_ok=True)

        # Start with the persisted value (if any). The file stores the last used N;
        # the next N to issue would be last_used + 1.
        next_from_file = 1
        if counter_file.exists():
            try:
                content = counter_file.read_text(encoding="utf-8").strip()
                if content:
                    next_from_file = int(content) + 1
            except Exception:
                # If the file is malformed, ignore and fall back to scanning manifests.
                next_from_file = 1

        # Scan all JSON files under SWAP Outputs for occurrences of slot IDs like "N0125"
        # and derive the highest numeric value seen. This catches per-gantry manifests,
        # the global manifest, pending manifest, and any run manifests.
        output_folder = project_path("SWAP Outputs")
        max_seen = 0
        if output_folder.exists():
            for json_path in output_folder.rglob("*.json"):
                try:
                    text = json_path.read_text(encoding="utf-8")
                except Exception:
                    continue
                for match in re.findall(r"N(\d{3,6})", text):
                    try:
                        val = int(match)
                        if val > max_seen:
                            max_seen = val
                    except ValueError:
                        continue

        next_from_manifests = max_seen + 1 if max_seen else 1

        # Choose the safest (highest) next starting N.
        return max(next_from_file, next_from_manifests)

    def _save_n_counter(self, last_n):
        counter_file = self._n_counter_file()
        counter_file.parent.mkdir(parents=True, exist_ok=True)
        counter_file.write_text(str(last_n), encoding="utf-8")

    def _collect_swap_parts(self, starting_n=1):
        print("\nAllowed parts:")
        print(", ".join(self.catalog.parts))
        print("\nEnter part names one by one. Type 'REFERENCE' for a reference label, 'done' when finished.")

        collected = []
        n_counter = starting_n
        while True:
            raw = self._prompt_input(f"Part {n_counter}:").strip().upper()
            if raw == "DONE":
                if not collected:
                    print("You must enter at least one part.")
                    continue
                break
            if not raw:
                print("Part cannot be empty.")
                continue

            slot_id = f"N{n_counter:04d}"

            if raw == "REFERENCE":
                collected.append({"part": "REFERENCE", "slot_id": slot_id, "n_number": n_counter, "is_reference": True})
                print(f"  -> {slot_id} = REFERENCE-{n_counter}")
                n_counter += 1
                continue

            if not self.catalog.contains(raw):
                print(f"{raw} is not currently in allowed parts.")
                should_add = self._ask_yes_no(
                    f"Add {raw} to {self.catalog.file_path}",
                    default="Y",
                )
                if not should_add:
                    continue
                self.catalog.add(raw)
                print(f"Added {raw} to allowed parts.")

            collected.append({"part": raw, "slot_id": slot_id, "n_number": n_counter})
            print(f"  -> {slot_id} = {raw}")
            n_counter += 1

        self.swap_parts = collected

        # Test: one label per unique part; production: cycle to fill all 117 slots
        slot_count = len(collected) if self.test_mode else MAX_LABELS_PER_PAGE
        self.slot_sequence = [
            collected[i % len(collected)] for i in range(slot_count)
        ]

        if self.test_mode:
            print(f"\n{len(collected)} unique part(s) entered — test run will generate {len(collected)} label(s):")
        else:
            print(f"\n{len(collected)} unique part(s) entered, cycling to fill {MAX_LABELS_PER_PAGE} slots:")
        for slot in self.swap_parts:
            display = f"REFERENCE-{slot['n_number']}" if slot.get("is_reference") else slot["part"]
            print(f"  {slot['slot_id']} = {display}")

        self.aria_label_part = "SWAP"
        # Default style based on first part; _generate_page uses per-job style
        self.letter_spacing, self.rotation_angle = self._style_for_part(collected[0]["part"])
        self.counter_file = self._counter_file_for_part(self.aria_label_part)

    def _select_part(self):
        print("\nAllowed parts:")
        print(", ".join(self.catalog.parts))

        use_default = self._ask_yes_no(
            f"Use default starting part {self.default_part}",
            default="Y",
        )

        if use_default:
            chosen_part = self.default_part
        else:
            while True:
                chosen_part = self._prompt_input("Enter part (e.g., CH01503):").strip().upper()
                if not chosen_part:
                    print("Part cannot be empty.")
                    continue

                if self.catalog.contains(chosen_part):
                    break

                print(f"{chosen_part} is not currently in allowed parts.")
                should_add = self._ask_yes_no(
                    f"Add {chosen_part} to {self.catalog.file_path}",
                    default="Y",
                )
                if should_add:
                    self.catalog.add(chosen_part)
                    print(f"Added {chosen_part} to allowed parts.")
                    break

        self.aria_label_part = chosen_part
        self.letter_spacing, self.rotation_angle = self._style_for_part(self.aria_label_part)
        self.counter_file = self._counter_file_for_part(self.aria_label_part)

    def get_part_number(self):
        self.counter_file.parent.mkdir(exist_ok=True)
        legacy_counter_file = project_path(f"{self.aria_label_part}.txt")

        if not os.path.exists(self.counter_file) and os.path.exists(legacy_counter_file):
            with open(legacy_counter_file, "r", encoding="utf-8") as f:
                legacy_content = f.read().strip()
            with open(self.counter_file, "w", encoding="utf-8") as f:
                f.write(legacy_content or "0")

        if os.path.exists(self.counter_file):
            with open(self.counter_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            self.aria_label_number = int(content) if content else 0
        else:
            self.aria_label_number = 0
        return self.aria_label_number

    @staticmethod
    def _label_geometry():
        columns = GRID_COLUMNS
        rows = GRID_ROWS
        
        radius =  13 # OG 12
        x_start = 120 # For any OG recall 10by10
        x_end =   120 + 576.8 # Orignal 607.2 and 609.2 is too far so 608? 608.4 might have pushed it a little too into where the ppr is, because 104.2 is still perfect and readable
        # Goat is 576, testing
        y_start = 95.8 # goat was 96.2
        y_end =   95.8 + 865    # Goat is 864, testing 865
        
        x_spacing = (x_end - x_start) / (columns - 1)
        y_spacing = (y_end - y_start) / (rows - 1)
        return columns, radius, x_start, y_start, x_spacing, y_spacing

    @staticmethod
    def _build_label_id(part, serial_number):
        return f"{part}-{serial_number:06d}"

    @staticmethod
    def _build_data_matrix_id(serial_number):
        # Keep the Data Matrix compact, but leave it human-readable and decimal.
        # The printed label still carries the part name; the scan carries the padded serial.
        return f"{serial_number:06d}"

    @staticmethod
    def _build_padded_serial(serial_number):
        return f"{serial_number:06d}"

    def _build_tool_records(self):
        tools = []
        for slot in self.swap_parts:
            n = slot["n_number"]
            tools.append({
                "tool_number":  slot["slot_id"],
                "tool_name":    slot["part"],
                "gantry":       self.gantry,
                "serial_id":    n,
                "padded_serial": f"{n:04d}",
            })
        return tools

    def _build_page_jobs(self, labels_on_page, output_svg, page_number):
        columns, radius, x_start, y_start, x_spacing, y_spacing = self._label_geometry()
        jobs = []

        for i in range(labels_on_page):
            slot = self.slot_sequence[i]
            col = i % columns
            row = i // columns
            cx = x_start + col * x_spacing
            cy = y_start + row * y_spacing
            if slot.get("is_reference"):
                label_text = f"REFERENCE-{slot['n_number']}"
            else:
                label_text = f"{slot['part']}-{slot['n_number']:04d}"
            letter_spacing, rotation_angle = self._style_for_part(slot["part"])
            jobs.append(
                {
                    "index": i,
                    "serial_number": i + 1,
                    "cx": cx,
                    "cy": cy,
                    "radius": radius,
                    "path_id": f"CircleTextPath{page_number}_{i}",
                    "label_text": label_text,
                    "is_reference": slot.get("is_reference", False),
                    "barcode_data": slot["slot_id"],
                    "letter_spacing": letter_spacing,
                    "rotation_angle": rotation_angle,
                    "verification_context": {
                        "label_id": label_text,
                        "data_matrix_id": slot["slot_id"],
                        "barcode_data": slot["slot_id"],
                        "part": slot["part"],
                        "slot_id": slot["slot_id"],
                        "page_number": page_number,
                        "position_on_page": i + 1,
                        "output_file": output_svg,
                    },
                }
            )
        return jobs

    def _production_output_folder(self):
        if self.gantry:
            return project_path("SWAP Outputs") / self.gantry
        return project_path(f"{self.aria_label_part} Outputs")

    def _counter_file_for_part(self, part):
        return project_path(f"{part} Outputs") / f"{part}.txt"

    def _next_production_output_version(self):
        output_folder = self._production_output_folder()
        output_folder.mkdir(parents=True, exist_ok=True)

        prefix = self.gantry if self.gantry else self.aria_label_part
        version_pattern = re.compile(
            rf"^{re.escape(prefix)}_V(\d+)\.svg$",
            re.IGNORECASE,
        )
        highest_version = 0

        for output_file in output_folder.glob(f"{prefix}_V*.svg"):
            match = version_pattern.match(output_file.name)
            if match:
                highest_version = max(highest_version, int(match.group(1)))

        return highest_version + 1

    def _build_output_name(self, test_mode, page_number, total_pages, starting_version=None):
        if test_mode:
            if total_pages == 1:
                return "label_test.svg"
            return f"label_test_{page_number}.svg"

        version_number = starting_version + page_number - 1
        output_folder = self._production_output_folder()
        prefix = self.gantry if self.gantry else self.aria_label_part
        return str(output_folder / f"{prefix}_V{version_number}.svg")

    def _build_database_manifest(self, generated_files):
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "event_type": "pico_labels.generated",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "test" if self.test_mode else "production",
            "gantry": self.gantry,
            "tools": self._build_tool_records(),
            "files": {
                "generated_svgs": generated_files,
                "template_file": str(self.template_path),
                "verification_report": str(project_path(VERIFICATION_REPORT_FILE)),
            },
        }

    def _write_database_manifest(self, manifest):
        if self.test_mode:
            manifest_path = project_path("label_test_manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            return str(manifest_path)

        # Per-gantry manifest: SWAP Outputs/{gantry}/{gantry}_manifest.json
        gantry_folder = project_path("SWAP Outputs") / self.gantry
        gantry_folder.mkdir(parents=True, exist_ok=True)
        gantry_manifest_path = gantry_folder / f"{self.gantry}_manifest.json"

        existing_gantry = {}
        if gantry_manifest_path.exists():
            with open(gantry_manifest_path, "r", encoding="utf-8") as f:
                existing_gantry = json.load(f)

        existing_runs = existing_gantry.get("runs", [])
        existing_tools = existing_gantry.get("tools", [])
        known_tool_numbers = {t["tool_number"] for t in existing_tools}
        new_tools = [t for t in manifest["tools"] if t["tool_number"] not in known_tool_numbers]

        manifest["files"]["manifest_file"] = str(gantry_manifest_path)
        gantry_combined = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "event_type": "pico_labels.gantry_manifest",
            "gantry": self.gantry,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "runs": [*existing_runs, manifest],
            "tools": [*existing_tools, *new_tools],
        }

        with open(gantry_manifest_path, "w", encoding="utf-8") as f:
            json.dump(gantry_combined, f, indent=2)

        # Global manifest: SWAP Outputs/global_manifest.json
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        existing_global = {}
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                existing_global = json.load(f)

        gantries = existing_global.get("gantries", {})
        gantry_existing_tools = gantries.get(self.gantry, [])
        known_global_tool_numbers = {t["tool_number"] for t in gantry_existing_tools}
        new_global_tools = [t for t in manifest["tools"] if t["tool_number"] not in known_global_tool_numbers]
        gantries[self.gantry] = [*gantry_existing_tools, *new_global_tools]

        global_manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "event_type": "pico_labels.global_manifest",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "gantries": gantries,
        }

        with open(global_manifest_path, "w", encoding="utf-8") as f:
            json.dump(global_manifest, f, indent=2)

        return str(gantry_manifest_path)

    def _generate_page(self, labels_on_page, output_svg, page_number):
        root = deepcopy(self._template_root)
        barcode_jobs = self._build_page_jobs(labels_on_page, output_svg, page_number)
        rendered_barcodes = self.renderer.render_many(barcode_jobs)

        for job, (barcode_image, base64_str) in zip(barcode_jobs, rendered_barcodes):
            circular_path = ET.Element(
                f"{{{SVG_NAMESPACE}}}path",
                {
                    "id": job["path_id"],
                    "d": (
                        f"M {job['cx']} {job['cy']} m -{job['radius']},0 "
                        f"a {job['radius']},{job['radius']} 0 1,1 {2 * job['radius']},0 "
                        f"a {job['radius']},{job['radius']} 0 1,1 -{2 * job['radius']},0"
                    ),
                    "fill": "none",
                    "transform": f"rotate({job.get('rotation_angle', self.rotation_angle)} {job['cx']} {job['cy']})",
                },
            )
            root.append(circular_path)

            text = ET.Element(
                f"{{{SVG_NAMESPACE}}}text",
                {
                    "font-size": "7", # OG 6.5
                    "fill": self.color,
                    "letter-spacing": str(job.get("letter_spacing", self.letter_spacing)),
                    "font-family": "Roboto Mono",
                },
            )
            text_path = ET.SubElement(
                text,
                f"{{{SVG_NAMESPACE}}}textPath",
                {"href": f"#{job['path_id']}"},
            )
            if job.get("is_reference"):
                prefix, number = job["label_text"].split("-", 1)
                text_path.text = prefix
                sep = ET.SubElement(text_path, f"{{{SVG_NAMESPACE}}}tspan", {"font-size": "12"})
                sep.text = "-"
                sep.tail = number
            else:
                text_path.text = job["label_text"]
            root.append(text)

            self.verifier.verify(
                barcode_image,
                job["barcode_data"],
                context=job["verification_context"],
            )
            barcode_elem = ET.Element(
                f"{{{SVG_NAMESPACE}}}image",
                {
                    "x": str(job["cx"] + 9.6), # 180° flip of original cx - 18.8 (center was -15.05 → now +15.05 from label center), 
                    # V17 is + 10.8, previously 10.4
                    "y": str(job["cy"] - 5), # 180° flip of original cy - 3 (center was +0.75 → now -0.75 from label center)
                    "width": "8.6", # 7.5 BEAST
                    "height": "8.6", # 7.5 BEAST
                    "href": f"data:image/png;base64,{base64_str}",
                    "transform": f"rotate(180, {job['cx'] + 10.8 + 3.75}, {job['cy'] - 4.5 + 3.75})",
                },
            )
            root.append(barcode_elem)

        self.aria_label_number += labels_on_page
        ET.ElementTree(root).write(project_path(output_svg))

    def generate_pages(self):
        total_pages = (self.labels_requested + MAX_LABELS_PER_PAGE - 1) // MAX_LABELS_PER_PAGE
        remaining = self.labels_requested
        generated_files = []
        starting_version = None
        if not self.test_mode:
            starting_version = self._next_production_output_version()

        for page_number in range(1, total_pages + 1):
            labels_on_page = min(MAX_LABELS_PER_PAGE, remaining)
            output_name = self._build_output_name(
                self.test_mode,
                page_number,
                total_pages,
                starting_version,
            )
            self._generate_page(labels_on_page, output_name, page_number)
            generated_files.append(output_name)
            remaining -= labels_on_page
            print(f"Wrote {output_name} with {labels_on_page} labels.")

        return generated_files

    def save_counter(self):
        self.counter_file.parent.mkdir(exist_ok=True)
        with open(self.counter_file, "w", encoding="utf-8") as f:
            f.write(str(self.aria_label_number))

    def _open_path(self, path):
        path = project_path(path)
        try:
            os.startfile(path)
            return True
        except OSError as exc:
            print(f"Could not open {path}: {exc}")
            return False

    def _post_generation_action(self, generated_files, manifest_path):
        if not generated_files:
            return

        first_svg = project_path(generated_files[0])
        output_folder = first_svg.parent

        print("\nPost-generation action:")
        print("1) Open first SVG")
        print("2) Open output folder")
        print("3) Open manifest JSON")
        print("4) Skip")

        while True:
            choice = self._prompt_input("Choose 1, 2, 3, or 4 [4]:").strip()
            if choice == "2":
                self._open_path(output_folder)
                return
            if choice == "1":
                self._open_path(first_svg)
                return
            if choice == "3":
                if manifest_path:
                    self._open_path(manifest_path)
                else:
                    print("No manifest JSON was created for this run.")
                return
            if choice in {"", "4"}:
                return
            print("Please enter 1, 2, 3, or 4.")

    def _remove_nozzle_ids(self):
        """Remove X Nozzle IDs starting from the last one created."""
        print("\n=== Remove Nozzle IDs ===")
        
        current_n = self._read_n_counter()
        print(f"Current N-counter: N{current_n:04d}")
        
        if current_n <= 0:
            print("No Nozzle IDs to remove (counter is at 0 or below).")
            return
        
        count = self._ask_positive_int("How many Nozzle IDs to remove", default=1)
        
        if count > current_n:
            print(f"Cannot remove {count} IDs. Only {current_n} IDs exist (N0001 through N{current_n:04d}).")
            return
        
        # Confirm action
        first_removed = current_n - count + 1
        last_removed = current_n
        confirm = self._ask_yes_no(
            f"Remove {count} Nozzle ID(s): N{first_removed:04d} through N{last_removed:04d}",
            default="N"
        )
        
        if not confirm:
            print("Removal cancelled.")
            return
        
        # Remove from manifests
        ids_to_remove = {f"N{i:04d}" for i in range(first_removed, last_removed + 1)}
        self._remove_ids_from_manifests(ids_to_remove)
        
        # Update counter
        new_n = current_n - count
        self._save_n_counter(new_n)
        print(f"✓ Removed {count} Nozzle ID(s)")
        print(f"✓ N-counter updated: N{current_n:04d} → N{new_n:04d}")
        print(f"  Next Nozzle ID will be: N{new_n + 1:04d}")

    def _remove_ids_from_manifests(self, ids_to_remove):
        """Remove specified Nozzle IDs from all manifest files."""
        # Update global manifest
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                global_manifest = json.load(f)
            
            # Remove tools with matching slot_ids
            for gantry in global_manifest.get("gantries", {}):
                tools = global_manifest["gantries"][gantry]
                filtered_tools = [t for t in tools if t.get("tool_number") not in ids_to_remove]
                global_manifest["gantries"][gantry] = filtered_tools
            
            global_manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            with open(global_manifest_path, "w", encoding="utf-8") as f:
                json.dump(global_manifest, f, indent=2)
            print(f"✓ Updated global manifest")
        
        # Update per-gantry manifests
        swap_outputs = project_path("SWAP Outputs")
        if swap_outputs.exists():
            for gantry_dir in swap_outputs.iterdir():
                if not gantry_dir.is_dir() or gantry_dir.name in {"G1", "G2", "G3", "G4", "G5"}:
                    gantry_manifest_path = gantry_dir / f"{gantry_dir.name}_manifest.json"
                    if gantry_manifest_path.exists():
                        with open(gantry_manifest_path, "r", encoding="utf-8") as f:
                            gantry_manifest = json.load(f)
                        
                        # Remove tools and runs that used these IDs
                        tools = gantry_manifest.get("tools", [])
                        filtered_tools = [t for t in tools if t.get("tool_number") not in ids_to_remove]
                        gantry_manifest["tools"] = filtered_tools
                        
                        gantry_manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
                        with open(gantry_manifest_path, "w", encoding="utf-8") as f:
                            json.dump(gantry_manifest, f, indent=2)
                        print(f"✓ Updated {gantry_dir.name} manifest")

    def _main_menu(self):
        """Main menu to choose between generating or removing Nozzle IDs."""
        print("\n=== PiCo Label Generator ===")
        print("1) Generate new labels")
        print("2) Manage tools (unassign, move, delete)")
        print("3) Remove Nozzle IDs (legacy)")
        
        while True:
            choice = self._prompt_input("Choose 1, 2, or 3 [1]:").strip()
            if choice in {"", "1"}:
                return "generate"
            if choice == "2":
                return "manage"
            if choice == "3":
                return "remove"
            print("Please enter 1, 2, or 3.")

    def _pending_manifest_path(self):
        """Path to pending tools manifest (not assigned to any gantry)."""
        return project_path("SWAP Outputs") / "pending_tools_manifest.json"

    def _load_pending_manifest(self):
        """Load pending tools manifest."""
        path = self._pending_manifest_path()
        if not path.exists():
            return {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "event_type": "pico_labels.pending_tools_manifest",
                "tools": [],
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_pending_manifest(self, manifest):
        """Save pending tools manifest."""
        path = self._pending_manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    def _get_all_tools(self):
        """Get all tools organized by location (gantry or pending)."""
        tools_by_location = {
            "pending": [],
            "G1": [], "G2": [], "G3": [], "G4": [], "G5": []
        }
        
        # Pending tools
        pending_manifest = self._load_pending_manifest()
        for tool in pending_manifest.get("tools", []):
            tools_by_location["pending"].append(tool)
        
        # Gantry tools
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                global_manifest = json.load(f)
            
            for gantry, tools in global_manifest.get("gantries", {}).items():
                if gantry in tools_by_location:
                    tools_by_location[gantry] = tools
        
        return tools_by_location

    def _find_tool_location(self, tool_number):
        """Find which gantry or pending a tool is assigned to. Returns (location_type, location_name)."""
        tools_by_location = self._get_all_tools()
        
        for location, tools in tools_by_location.items():
            for tool in tools:
                if tool.get("tool_number") == tool_number:
                    return (location, tools)
        
        return (None, None)

    def _list_all_tools(self):
        """Display all tools organized by location."""
        print("\n=== Tool Inventory ===\n")
        tools_by_location = self._get_all_tools()
        
        total = 0
        for location in ["pending", "G1", "G2", "G3", "G4", "G5"]:
            tools = tools_by_location[location]
            status = "PENDING (not in production)" if location == "pending" else f"GANTRY {location}"
            print(f"{status}: {len(tools)} tool(s)")
            
            for tool in sorted(tools, key=lambda t: t.get("tool_number", "")):
                tool_num = tool.get("tool_number", "?")
                tool_name = tool.get("tool_name", "?")
                serial = tool.get("serial_id", "?")
                print(f"  {tool_num} -> {tool_name} (serial: {serial})")
                total += 1
        
        print(f"\nTotal: {total} tool(s)\n")

    def _unassign_tool_from_gantry(self, tool_number=None, gantry=None):
        """Unassign a tool from its gantry and move it to pending."""
        if tool_number is None:
            tool_number = self._prompt_input("Enter tool number to unassign (e.g., N0021):").strip().upper()
        
        location, tools_list = self._find_tool_location(tool_number)
        
        if location is None:
            print(f"✗ Tool {tool_number} not found in any gantry or pending.")
            return False
        
        if location == "pending":
            print(f"✗ Tool {tool_number} is already in pending (not assigned to any gantry).")
            return False
        
        if gantry and location != gantry:
            print(f"✗ Tool {tool_number} is in {location}, not {gantry}.")
            return False
        
        # Find the tool
        tool_to_move = None
        for tool in tools_list:
            if tool.get("tool_number") == tool_number:
                tool_to_move = tool
                break
        
        if tool_to_move is None:
            print(f"✗ Could not find tool {tool_number}.")
            return False
        
        confirm = self._ask_yes_no(
            f"Unassign {tool_number} ({tool_to_move.get('tool_name')}) from {location}",
            default="N"
        )
        
        if not confirm:
            print("Unassign cancelled.")
            return False
        
        # Remove from gantry
        gantry_manifest_path = project_path("SWAP Outputs") / location / f"{location}_manifest.json"
        if gantry_manifest_path.exists():
            with open(gantry_manifest_path, "r", encoding="utf-8") as f:
                gantry_manifest = json.load(f)
            
            tools = gantry_manifest.get("tools", [])
            filtered_tools = [t for t in tools if t.get("tool_number") != tool_number]
            gantry_manifest["tools"] = filtered_tools
            
            with open(gantry_manifest_path, "w", encoding="utf-8") as f:
                json.dump(gantry_manifest, f, indent=2)
        
        # Remove from global manifest
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                global_manifest = json.load(f)
            
            gantries = global_manifest.get("gantries", {})
            if location in gantries:
                gantries[location] = [t for t in gantries[location] if t.get("tool_number") != tool_number]
            
            with open(global_manifest_path, "w", encoding="utf-8") as f:
                json.dump(global_manifest, f, indent=2)
        
        # Add to pending
        pending_manifest = self._load_pending_manifest()
        pending_manifest["tools"].append(tool_to_move)
        self._save_pending_manifest(pending_manifest)
        
        print(f"✓ Unassigned {tool_number} from {location}")
        print(f"✓ Tool moved to pending")
        return True

    def _move_tool_to_gantry(self, tool_number=None, target_gantry=None):
        """Move a tool from pending to a specific gantry."""
        if tool_number is None:
            tool_number = self._prompt_input("Enter tool number to move (e.g., N0021):").strip().upper()
        
        location, _ = self._find_tool_location(tool_number)
        
        if location is None:
            print(f"✗ Tool {tool_number} not found.")
            return False
        
        if location != "pending":
            print(f"✗ Tool {tool_number} is already in {location} (not pending).")
            return False
        
        # Get the tool
        pending_manifest = self._load_pending_manifest()
        tool_to_move = None
        for tool in pending_manifest.get("tools", []):
            if tool.get("tool_number") == tool_number:
                tool_to_move = tool
                break
        
        if tool_to_move is None:
            print(f"✗ Could not find tool {tool_number} in pending.")
            return False
        
        # Ask for target gantry
        if target_gantry is None:
            valid_gantries = {"G1", "G2", "G3", "G4", "G5"}
            while True:
                target_gantry = self._prompt_input("Target gantry (G1, G2, G3, G4, G5):").strip().upper()
                if target_gantry in valid_gantries:
                    break
                print(f"Please enter one of: {', '.join(sorted(valid_gantries))}")
        
        confirm = self._ask_yes_no(
            f"Move {tool_number} ({tool_to_move.get('tool_name')}) to {target_gantry}",
            default="Y"
        )
        
        if not confirm:
            print("Move cancelled.")
            return False
        
        # Update tool gantry
        tool_to_move["gantry"] = target_gantry
        
        # Add to gantry
        gantry_manifest_path = project_path("SWAP Outputs") / target_gantry / f"{target_gantry}_manifest.json"
        gantry_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        
        if gantry_manifest_path.exists():
            with open(gantry_manifest_path, "r", encoding="utf-8") as f:
                gantry_manifest = json.load(f)
        else:
            gantry_manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "event_type": "pico_labels.gantry_manifest",
                "gantry": target_gantry,
                "runs": [],
                "tools": [],
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        
        tools = gantry_manifest.get("tools", [])
        if not any(t.get("tool_number") == tool_number for t in tools):
            tools.append(tool_to_move)
            gantry_manifest["tools"] = tools
        
        with open(gantry_manifest_path, "w", encoding="utf-8") as f:
            json.dump(gantry_manifest, f, indent=2)
        
        # Add to global manifest
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                global_manifest = json.load(f)
        else:
            global_manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "event_type": "pico_labels.global_manifest",
                "gantries": {},
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        
        gantries = global_manifest.get("gantries", {})
        if target_gantry not in gantries:
            gantries[target_gantry] = []
        
        if not any(t.get("tool_number") == tool_number for t in gantries[target_gantry]):
            gantries[target_gantry].append(tool_to_move)
        
        global_manifest["gantries"] = gantries
        with open(global_manifest_path, "w", encoding="utf-8") as f:
            json.dump(global_manifest, f, indent=2)
        
        # Remove from pending
        pending_manifest["tools"] = [t for t in pending_manifest.get("tools", []) if t.get("tool_number") != tool_number]
        self._save_pending_manifest(pending_manifest)
        
        print(f"✓ Moved {tool_number} from pending to {target_gantry}")
        return True

    def _delete_tool_entirely(self, tool_number=None):
        """Delete a tool completely from all manifests."""
        if tool_number is None:
            tool_number = self._prompt_input("Enter tool number to delete (e.g., N0021):").strip().upper()
        
        location, _ = self._find_tool_location(tool_number)
        
        if location is None:
            print(f"✗ Tool {tool_number} not found.")
            return False
        
        confirm = self._ask_yes_no(
            f"PERMANENTLY DELETE {tool_number} from {location}",
            default="N"
        )
        
        if not confirm:
            print("Delete cancelled.")
            return False
        
        # Remove from pending
        if location == "pending":
            pending_manifest = self._load_pending_manifest()
            pending_manifest["tools"] = [t for t in pending_manifest.get("tools", []) if t.get("tool_number") != tool_number]
            self._save_pending_manifest(pending_manifest)
        else:
            # Remove from gantry
            gantry_manifest_path = project_path("SWAP Outputs") / location / f"{location}_manifest.json"
            if gantry_manifest_path.exists():
                with open(gantry_manifest_path, "r", encoding="utf-8") as f:
                    gantry_manifest = json.load(f)
                
                tools = gantry_manifest.get("tools", [])
                gantry_manifest["tools"] = [t for t in tools if t.get("tool_number") != tool_number]
                
                with open(gantry_manifest_path, "w", encoding="utf-8") as f:
                    json.dump(gantry_manifest, f, indent=2)
        
        # Remove from global manifest
        global_manifest_path = project_path("SWAP Outputs") / "global_manifest.json"
        if global_manifest_path.exists():
            with open(global_manifest_path, "r", encoding="utf-8") as f:
                global_manifest = json.load(f)
            
            for gantry in global_manifest.get("gantries", {}):
                global_manifest["gantries"][gantry] = [t for t in global_manifest["gantries"][gantry] if t.get("tool_number") != tool_number]
            
            with open(global_manifest_path, "w", encoding="utf-8") as f:
                json.dump(global_manifest, f, indent=2)
        
        print(f"✓ Permanently deleted {tool_number}")
        return True

    def _management_menu(self):
        """Interactive menu for tool management."""
        while True:
            print("\n=== Tool Management ===")
            print("1) List all tools")
            print("2) Unassign tool from gantry (move to pending)")
            print("3) Move tool from pending to gantry")
            print("4) Delete tool entirely")
            print("5) Back to main menu")
            
            choice = self._prompt_input("Choose 1-5:").strip()
            
            if choice == "1":
                self._list_all_tools()
            elif choice == "2":
                self._unassign_tool_from_gantry()
            elif choice == "3":
                self._move_tool_to_gantry()
            elif choice == "4":
                self._delete_tool_entirely()
            elif choice == "5":
                break
            else:
                print("Please enter 1-5.")

    def run(self):
        action = self._main_menu()
        
        if action == "manage":
            self._management_menu()
            return
        
        if action == "remove":
            self._remove_nozzle_ids()
            return
        
        # Generate path
        print("Label Generation Setup")
        self._configure_mode()

        # Production: ask gantry and load persistent N-counter before collecting parts
        starting_n = 1
        if not self.test_mode:
            self._select_gantry()
            starting_n = self._read_n_counter()
            print(f"N-counter continuing from N{starting_n:04d}.")

        self._collect_swap_parts(starting_n=starting_n)

        # Test: one label per unique part; production: full 117-slot page
        self.labels_requested = len(self.swap_parts) if self.test_mode else MAX_LABELS_PER_PAGE
        print(f"This run will create {self.labels_requested} label(s).")

        self._template_root = self._load_template_root()
        self.aria_label_number = 0
        self.verifier.run_health_check()

        generated_files = self.generate_pages()

        # Persist the last N-number used so the next production run continues from here
        if not self.test_mode:
            last_n = starting_n + len(self.swap_parts) - 1
            self._save_n_counter(last_n)
            print(f"N-counter saved at N{last_n:04d} (next run starts at N{last_n + 1:04d}).")

        database_manifest = self._build_database_manifest(generated_files)
        manifest_path = self._write_database_manifest(database_manifest)

        report = self.verifier.write_report(
            VERIFICATION_REPORT_FILE,
            run_context={
                "mode": "test" if self.test_mode else "production",
                "gantry": self.gantry,
                "swap_parts": self.swap_parts,
                "verification_scope": self.verifier.verification_scope,
                "labels_requested": self.labels_requested,
                "generated_files": generated_files,
                "database_manifest_file": manifest_path,
                "database_manifest": database_manifest,
            },
        )
        summary = report["summary"]

        print("\nRun complete.")
        if self.gantry:
            print(f"Gantry: {self.gantry}")
        print(f"Generated files: {', '.join(generated_files)}")
        print(
            "Swap parts: "
            + ", ".join(
                f"{s['slot_id']}=REFERENCE-{s['n_number']}" if s.get("is_reference") else f"{s['slot_id']}={s['part']}"
                for s in self.swap_parts
            )
        )
        print(f"Verification result: {report['verification_result'].upper()}")
        print(
            "Verification summary: "
            f"pass={summary['pass']}, fail={summary['fail']}, "
            f"skipped={summary['skipped']}, labels_seen={summary['labels_seen']}, "
            f"labels_checked={summary['labels_checked']}"
        )
        print(f"Verification report: {VERIFICATION_REPORT_FILE}")
        self._post_generation_action(generated_files, manifest_path)


if __name__ == "__main__":
    generator = Label_Gen()
    
    # Parse command-line arguments for tool management
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        
        # Handle --unassign N0021 --from G2
        if "--unassign" in args:
            idx = args.index("--unassign")
            if idx + 1 < len(args):
                tool_num = args[idx + 1]
                gantry = None
                
                if "--from" in args:
                    gantry_idx = args.index("--from")
                    if gantry_idx + 1 < len(args):
                        gantry = args[gantry_idx + 1].upper()
                
                print(f"Unassigning {tool_num}" + (f" from {gantry}" if gantry else ""))
                generator._unassign_tool_from_gantry(tool_num, gantry)
            else:
                print("Error: --unassign requires a tool number")
        
        # Handle --move N0021 --to G2
        elif "--move" in args:
            idx = args.index("--move")
            if idx + 1 < len(args):
                tool_num = args[idx + 1]
                target_gantry = None
                
                if "--to" in args:
                    to_idx = args.index("--to")
                    if to_idx + 1 < len(args):
                        target_gantry = args[to_idx + 1].upper()
                
                print(f"Moving {tool_num}" + (f" to {target_gantry}" if target_gantry else ""))
                generator._move_tool_to_gantry(tool_num, target_gantry)
            else:
                print("Error: --move requires a tool number")
        
        # Handle --delete N0021
        elif "--delete" in args:
            idx = args.index("--delete")
            if idx + 1 < len(args):
                tool_num = args[idx + 1]
                print(f"Deleting {tool_num}")
                generator._delete_tool_entirely(tool_num)
            else:
                print("Error: --delete requires a tool number")
        
        # Handle --list
        elif "--list" in args:
            generator._list_all_tools()
        
        else:
            print("PiCo Label Generator")
            print("\nUsage:")
            print("  python PiCo_LabelsV21.py                          # Interactive mode")
            print("  python PiCo_LabelsV21.py --list                   # List all tools")
            print("  python PiCo_LabelsV21.py --unassign N0021         # Unassign tool")
            print("  python PiCo_LabelsV21.py --unassign N0021 --from G2  # Unassign from specific gantry")
            print("  python PiCo_LabelsV21.py --move N0021 --to G2     # Move tool to gantry")
            print("  python PiCo_LabelsV21.py --delete N0021           # Delete tool entirely")
    else:
        # Interactive mode
        generator.run()
