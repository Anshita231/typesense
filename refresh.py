"""
Typesense sync job (spec / temp / vmi_tags collections).

Converted from the notebook so it can run unattended via a .bat file + Task Scheduler.
The ONLY structural change vs. the notebook: filenames are generated from today's
date automatically, so nothing needs to be hand-edited before each run.

Everything downstream (SQL, normalization, grouping, upload) is unchanged.
"""

import os
import re
import sys
import json
import time
import traceback
from datetime import datetime, timedelta

import psycopg2
import pandas as pd
import warnings
import typesense

import attribute_matching

warnings.filterwarnings("ignore", category=UserWarning)

# ---------------------------------------------------------------------------
# CONFIG  — the only things you might ever touch
# ---------------------------------------------------------------------------

# Where the intermediate .jsonl files get written.
# Defaults to a "data" folder next to this script.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# True  -> filenames get today's date (MainData_27July.jsonl) and pile up as history
# False -> fixed names (MainData.jsonl), overwritten each run, no clutter
USE_DATED_FILENAMES = True

# If dated files are kept, delete ones older than this many days (0 = keep forever)
RETENTION_DAYS = 5

# Credentials: prefer environment variables, fall back to inline values.
# (Fill the fallbacks in, OR set the env vars in the .bat file.)
TYPESENSE_HOST    = os.environ.get("TYPESENSE_HOST", "")
TYPESENSE_API_KEY = os.environ.get("TYPESENSE_API_KEY", "")   # <-- put your key here or in the .bat

PG_HOST     = os.environ.get("PG_HOST", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "")
PG_USER     = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")        # <-- put your password here or in the .bat
PG_PORT     = int(os.environ.get("PG_PORT", ""))

# ---------------------------------------------------------------------------
# Filename helper — this is the piece that solves the "change name daily" problem
# ---------------------------------------------------------------------------

os.makedirs(DATA_DIR, exist_ok=True)

# e.g. "27July"  (matches your existing naming: day + full month name)
DATE_TAG = datetime.now().strftime("%d%B")


def path_for(stem: str) -> str:
    """Build a full path for an intermediate file, dated or fixed per config."""
    name = f"{stem}_{DATE_TAG}.jsonl" if USE_DATED_FILENAMES else f"{stem}.jsonl"
    return os.path.join(DATA_DIR, name)


# Every filename in the whole job now comes from these — nothing hardcoded.
MAIN_RAW      = path_for("MainData")
MAIN_GROUPED  = path_for("MainData_grouped")
TEMP_RAW      = path_for("TempData")
TEMP_GROUPED  = path_for("TempData_grouped")
VMI_RAW       = path_for("VMIData")
VMI_GROUPED   = path_for("VMIData_grouped")


def cleanup_old_files():
    """Delete dated .jsonl files older than RETENTION_DAYS so disk doesn't fill up."""
    if not (USE_DATED_FILENAMES and RETENTION_DAYS > 0):
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for fname in os.listdir(DATA_DIR):
        if fname.endswith(".jsonl"):
            fpath = os.path.join(DATA_DIR, fname)
            try:
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    print(f"Cleaned old file: {fname}")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Normalization (unchanged from notebook)
# ---------------------------------------------------------------------------

def normalize_productSpecification(spec_text):
    if pd.isna(spec_text):
        return ""

    text = str(spec_text).lower().strip()

    text = text.replace(",", " ")
    text = re.sub(r'(\d+)traid\b', r'\1 traid', text)
    text = re.sub(r'(\d+)pair\b', r'\1 pair', text)
    text = text.replace("-", " ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = text.replace("mm2", "sqmm")

    number_words = ["core", "cores", "sqmm", "sq", "mm", "inch", "pin", "amp", "kv",
                    "v", "w", "ltr", "kg", "gm", "m", "meter", "amp", "amps"]
    words_pattern = "|".join(map(re.escape, number_words))

    text = re.sub(rf'(\d+(?:\.\d+)?)(?=({words_pattern})\b)', r'\1 ', text, flags=re.IGNORECASE)
    text = re.sub(rf'\b({words_pattern})(?=\d)', r'\1 ', text, flags=re.IGNORECASE)

    text = re.sub(r'\bm(\d+)\b', r'm \1', text)
    text = re.sub(r'([a-z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([a-z])', r'\1 \2', text)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*[x*×]\s*(\d+(?:\.\d+)?)\b', r'\1 \2', text, flags=re.IGNORECASE)
    text = text.replace('"', ' inch')
    text = re.sub(r'(\d+/\d+)\s+[x*×]\s+(\d+)', r'\1 \2', text, flags=re.IGNORECASE)
    text = text.replace("&", " and ")
    text = re.sub(r'([a-z])/([a-z])', r'\1 \2', text)
    text = text.replace("+", " ")
    text = text.replace("@", " at ")
    text = text.replace(":", " ")
    text = text.replace(";", " ")
    text = re.sub(r'\s*[x*×]\s*', ' ', text)
    text = re.sub(r'(\d+/\d+)', r' \1 ', text)

    abbreviations = {
        'sq.mm': 'sqmm', 'sqmillimeter': 'sqmm', 'watt': 'w', 'wats': 'w', 'watts': 'w',
        'amp': 'amp', 'amps': 'amp', 'ampere': 'amp', 'litre': 'ltr', 'liter': 'ltr',
        'kg': 'kg', 'gram': 'gm', 'grams': 'gm', 'metre': 'mt', 'meter': 'mt',
        'meters': 'mt', 'metres': 'mt',
    }
    for old, new in abbreviations.items():
        text = re.sub(rf'\b{old}\b', new, text)

    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    return text


# ---------------------------------------------------------------------------
# Model number extraction (self-contained port of the identifier-extraction
# logic in app.py / search_app.py's parse_query — kept in sync manually).
# Used to derive the "modelNos" collection from each material's own
# productSpecification, so what gets stored here matches what a buyer's
# search query is checked against at query time.
# ---------------------------------------------------------------------------

BRANDS_XLSX = os.path.join(DATA_DIR, "Brands.xlsx")
ATTRIBUTES_XLSX = os.path.join(DATA_DIR, "Attributes.xlsx")


def _load_word_set(path, label):
    try:
        df = pd.read_excel(path)
        return {str(x).strip().lower() for x in df.iloc[:, 0].dropna()}
    except Exception as e:
        print(f"WARNING: could not load {label} from {path} ({e}) — "
              f"model number extraction will run without {label} filtering.")
        return set()


KNOWN_BRANDS = _load_word_set(BRANDS_XLSX, "brands")
KNOWN_ATTRIBUTES = _load_word_set(ATTRIBUTES_XLSX, "attributes")

GENERIC_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "of", "with", "in", "on", "at", "to", "by",
    "please", "need", "want", "buy", "price", "rate", "cost", "available",
    "item", "product", "piece", "pcs", "pc", "qty", "quantity", "required",
    "urgent", "new", "old", "no", "number", "model", "part", "code", "type", "size",
}

_IDENTIFIER_KEYWORD_RE = re.compile(
    r'\b(?:'
    r'model\s*(?:no\.?|number)?|'
    r'part\s*(?:no\.?|number|code)?|'
    r'product\s*(?:no\.?|number|code)?|'
    r'p\.\s*no\.?|'
    r'pn\.?|'
    r'item\s*code|'
    r'code|'
    r'no \.?|'
    r'number'
    r')\s*[:#-]?\s*(.+)',
    re.IGNORECASE
)


def extract_model_number(spec_text):
    """
    Pull "MODEL NO X" (or PART NO / PART CODE / etc.) out of a material's
    productSpecification. Same tail-walk logic as parse_query's
    identifier_match handling: walks tokens after the keyword, skipping
    leading descriptive words to find where the code actually starts,
    stopping at a brand/attribute/stopword or a "/" that leads into a
    description rather than more of the code.
    """
    if not spec_text:
        return None

    query_lower = str(spec_text).strip().lower()
    identifier_match = _IDENTIFIER_KEYWORD_RE.search(query_lower)
    if not identifier_match:
        return None

    tail_tokens = identifier_match.group(1).split()

    id_tokens = []
    for i, tok in enumerate(tail_tokens):
        has_terminator = bool(re.search(r'[,;]', tok))
        clean = tok.strip(",.;:")
        if not clean:
            break

        is_blocked = clean in KNOWN_BRANDS or clean in KNOWN_ATTRIBUTES or clean in GENERIC_STOPWORDS
        fails_charset = not re.fullmatch(r'[a-z0-9\-/]+', clean)

        if not id_tokens:
            # Still looking for the start of the code.
            if is_blocked or fails_charset:
                if i >= 4:
                    break
                continue
            has_digit_here = any(ch.isdigit() for ch in clean)
            next_tok = tail_tokens[i + 1].strip(",.;:") if i + 1 < len(tail_tokens) else ""
            next_has_digit = any(ch.isdigit() for ch in next_tok)
            if not has_digit_here and not next_has_digit:
                if i >= 4:
                    break
                continue
        else:
            if is_blocked or fails_charset:
                break

        if '/' in clean:
            pre, _, post = clean.partition('/')
            if not pre:
                if id_tokens:
                    break
                continue
            if not any(ch.isdigit() for ch in post):
                id_tokens.append(pre)
                break

        id_tokens.append(clean)
        if has_terminator:
            break

    return " ".join(id_tokens) if id_tokens else None


# ---------------------------------------------------------------------------
# Upload helper (retry logic from your VMI cell, reused everywhere)
# ---------------------------------------------------------------------------

def upload_file(client, collection, filepath, batch_size=5000, max_retries=5):
    """Stream a grouped .jsonl file into a Typesense collection in batches."""
    def _import(batch, start_line):
        for attempt in range(1, max_retries + 1):
            try:
                print(f"[{collection}] uploading {start_line}-{start_line + len(batch) - 1} (attempt {attempt})")
                client.collections[collection].documents.import_("".join(batch), {'action': 'upsert'})
                return True
            except Exception as e:
                print(f"  attempt {attempt} failed: {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                else:
                    print(f"  giving up on batch starting at {start_line}")
                    return False

    total, start_line, batch = 0, 1, []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            batch.append(line)
            if len(batch) == batch_size:
                if _import(batch, start_line):
                    total += len(batch)
                start_line += len(batch)
                batch = []
        if batch:
            if _import(batch, start_line):
                total += len(batch)
    print(f"[{collection}] finished: {total}")
    return total


# ---------------------------------------------------------------------------
# Deletion helper — removes documents from Typesense that no longer exist
# (or no longer qualify) on the Postgres side, so the collections don't
# accumulate stale materialIds over time.
# ---------------------------------------------------------------------------

def get_existing_ids(client, collection):
    """Fetch the set of all document ids currently in a Typesense collection."""
    ids = set()
    try:
        export_str = client.collections[collection].documents.export()
    except Exception as e:
        print(f"[{collection}] could not export existing ids, skipping delete step: {e}")
        return ids
    for line in export_str.splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in doc:
            ids.add(str(doc["id"]))
    return ids


def delete_stale_documents(client, collection, current_ids, batch_size=100):
    """
    Delete any document from `collection` whose id is not in `current_ids`
    (i.e. it was removed, or no longer matches the source query's filters,
    since the last sync). Deletes in batches via filter_by so a large
    number of stale ids doesn't blow up a single request.
    """
    existing_ids = get_existing_ids(client, collection)
    if not existing_ids:
        return 0

    current_ids = {str(i) for i in current_ids}
    stale_ids = list(existing_ids - current_ids)
    if not stale_ids:
        print(f"[{collection}] no stale documents to delete")
        return 0

    deleted = 0
    for i in range(0, len(stale_ids), batch_size):
        batch = stale_ids[i:i + batch_size]
        filter_str = "id:[" + ",".join(batch) + "]"
        try:
            result = client.collections[collection].documents.delete({"filter_by": filter_str})
            n = result.get("num_deleted", len(batch)) if isinstance(result, dict) else len(batch)
            deleted += n
            print(f"[{collection}] deleted {n} stale docs (batch {i // batch_size + 1})")
        except Exception as e:
            print(f"[{collection}] failed to delete batch starting at {i}: {e}")

    print(f"[{collection}] total stale deleted: {deleted} / {len(stale_ids)} candidates")
    return deleted


# ---------------------------------------------------------------------------
# Derived collections: modelNos + erp_code_maps.
#
# Both are built from data already fetched for spec/temp — modelNo from
# each material's own productSpecification, companyERPCode from the same
# companyERPCodes list run_main/run_temp already group per material — so
# no extra DB queries. Runs once, after BOTH spec and temp are grouped,
# since either can contribute rows to either derived collection; running
# it per-collection separately would make each call's delete_stale_documents
# wrongly treat the other collection's still-valid materials as stale.
# ---------------------------------------------------------------------------

def upsert_docs(client, collection, docs, batch_size=5000):
    """Upsert a list of dicts into a Typesense collection in batches."""
    total = 0
    for i in range(0, len(docs), batch_size):
        batch = docs[i:i + batch_size]
        try:
            client.collections[collection].documents.import_(batch, {"action": "upsert"})
            total += len(batch)
            print(f"[{collection}] uploaded {i}-{i + len(batch) - 1}")
        except Exception as e:
            print(f"[{collection}] batch starting at {i} failed: {e}")
    print(f"[{collection}] finished: {total}")
    return total


def run_derived_collections(client, grouped, grouped_temp):
    print("\n=== Derived: modelNos + erp_code_maps ===")

    model_no_docs = {}
    erp_code_docs = {}

    for doc in list(grouped.values()) + list(grouped_temp.values()):
        material_id = doc.get("materialId")
        if material_id is None:
            continue

        model_no = extract_model_number(doc.get("productSpecification"))
        if model_no:
            mid_str = str(material_id)
            model_no_docs[mid_str] = {
                "id": mid_str,
                "modelNo": model_no,
                "materialId": int(material_id)
            }

        for code in doc.get("companyERPCodes", []) or []:
            if not code:
                continue
            # id = the ERP code itself, so upserts naturally replace a
            # code's old materialId/spec if it ever gets remapped.
            erp_code_docs[str(code)] = {
                "id": str(code),
                "companyERPCode": code,
                "companyProdSpec": doc.get("productSpecification"),
                "materialId": int(material_id)
            }

    print("model numbers extracted:", len(model_no_docs))
    print("erp code mappings:", len(erp_code_docs))

    if model_no_docs:
        upsert_docs(client, "modelNos", list(model_no_docs.values()))
    delete_stale_documents(client, "modelNos", model_no_docs.keys())

    if erp_code_docs:
        upsert_docs(client, "erp_code_maps", list(erp_code_docs.values()))
    delete_stale_documents(client, "erp_code_maps", erp_code_docs.keys())

ATTRIBUTE_COLLECTION = "materialAttributes"


def build_attribute_doc(material_id, raw_product_spec_json):
    """
    Returns a materialAttributes document ({id, materialId, attr_...}) for
    one material, or None if nothing worth writing was found. Never
    touches the spec/temp document — this is written into its own
    collection separately, see run_material_attributes().
    """
    parsed = attribute_matching.parse_product_spec_json(raw_product_spec_json)
    fields = {}
    for key, entry in parsed.items():
        # Only the canonical keys we've actually reviewed and declared in
        # materialAttributes' schema — parse_product_spec_json() can
        # return long-tail keys too (unreviewed raw types), which aren't
        # declared anywhere and would fail to index if written.
        if key not in attribute_matching.KNOWN_ATTRIBUTE_KEYS:
            continue
        norm = entry.get("normalized")
        if not norm:
            continue
        fields[f"attr_{key}"] = norm["display"]
        if norm["numeric"] is not None:
            fields[f"attr_{key}_numeric"] = norm["numeric"]

    if not fields:
        return None

    mid_str = str(material_id)
    return {
        "id": mid_str,
        "materialId": int(material_id),
        # See create_attribute_collection.py — Typesense's reserved "id"
        # field can't be used as query_by, so this duplicate string field
        # exists purely to give a "q": "*" match-all search a valid
        # query_by target.
        "materialIdStr": mid_str,
        **fields
    }


def run_material_attributes(client, grouped, material_json, grouped_temp, temp_material_json):
    """
    Builds materialAttributes from BOTH spec and temp materials (a material
    could be in either) and syncs it — upsert what's current, delete
    what's now stale. Runs once, after both run_main and run_temp, for the
    same reason run_derived_collections does: running it per-collection
    separately would make each call's delete_stale_documents wrongly treat
    the other collection's still-valid materials as stale.
    """
    print(f"\n=== Derived: {ATTRIBUTE_COLLECTION} ===")

    docs = {}
    for mid, doc in grouped.items():
        raw_json = material_json.get(mid)
        attr_doc = build_attribute_doc(doc["materialId"], raw_json)
        if attr_doc:
            docs[attr_doc["id"]] = attr_doc
    for mid, doc in grouped_temp.items():
        raw_json = temp_material_json.get(mid)
        attr_doc = build_attribute_doc(doc["materialId"], raw_json)
        if attr_doc:
            docs[attr_doc["id"]] = attr_doc

    print(f"materials with at least one matched attribute: {len(docs)}")

    if docs:
        upsert_docs(client, ATTRIBUTE_COLLECTION, list(docs.values()))
    delete_stale_documents(client, ATTRIBUTE_COLLECTION, docs.keys())


SPEC_WITH_ATTRIBUTES_COLLECTION = "specWithAttributes"


def run_spec_with_attributes(client, grouped, material_json):
    """
    Builds specWithAttributes — STAGING/TEST collection only, spec/temp
    are never touched by this. MAIN (spec) materials only — temp is
    deliberately excluded. Each document is the FULL spec-shaped doc
    (same fields grouped[mid] already carries — productName, vendors,
    ARCvendors, everything) merged with that material's attr_* fields,
    so a single Typesense query against this collection can combine
    filter_by (attributes) with q (text) and let Typesense's own
    relevance engine rank by both together — the thing spec/temp +
    materialAttributes being separate collections can never do natively.

    isTemporary is set explicitly to "false" here, since this only ever
    processes main/spec materials.
    """
    print(f"\n=== Derived: {SPEC_WITH_ATTRIBUTES_COLLECTION} ===")

    docs = {}
    for mid, doc in grouped.items():
        raw_json = material_json.get(mid)
        attr_doc = build_attribute_doc(doc["materialId"], raw_json)
        merged = dict(doc)
        merged["isTemporary"] = "false"
        if attr_doc:
            for k, v in attr_doc.items():
                if k not in ("id", "materialId"):
                    merged[k] = v
        docs[merged["id"]] = merged

    print(f"materials in {SPEC_WITH_ATTRIBUTES_COLLECTION}: {len(docs)}")

    if docs:
        upsert_docs(client, SPEC_WITH_ATTRIBUTES_COLLECTION, list(docs.values()))
    delete_stale_documents(client, SPEC_WITH_ATTRIBUTES_COLLECTION, docs.keys())


# ---------------------------------------------------------------------------
# The three pipelines (SQL + grouping unchanged from notebook)
# ---------------------------------------------------------------------------

def run_main(conn, client):
    print("\n=== MAIN (spec) ===")
    query = """
    with cte as(
      select cerp."materialId", c."companyName", cb."branchName", brc."unitPrice", brc."validityPeriod",
             brc."leadTime",cerp."companyERPCode"
      from "buyerRateContracts" brc
      join "companyERPCodeMap" cerp on brc."companyERPCodeMapId" = cerp."companyERPCodeMapId"
      join "companies" c on c."companyId" = brc."companyId" and c."statusId" = '1'
      left join "companyBranches" cb on cb."branchId" = brc."branchId" and cb."statusId" = '1'
      where brc."statusId" = '1' and cerp."statusId" = '1'
    )
    select mm."materialId", mb."brandName", mc."categoryName", mm."productName", mm."variantName",
    mm."productSpecification", mm."productSpecificationJSON", mm."productDescription", mm."shortDescription",mm."listPrice", concat(mm."UOMValue", ' ', mm."UOM") as "UOM",
    case when vrci."materialId" is not null then 'VRC' end as "VRC", c."companyName", vrc."geoMapType", s."stateName",
    vrci."listPrice" as "vrcListPrice", vrci."discount", vrci."contractPrice", vrci."leadTime",
    case when cte."materialId" is not null then 'ARC' end as "ARC", cte."companyName" as "arcCompanyName",
    cte."branchName", cte."unitPrice", cte."validityPeriod", cte."companyERPCode", cte."leadTime" as "arcLeadTime"
    from "materialMaster" mm
    left join "vendorRateContractItem" vrci on vrci."materialId" = mm."materialId" and vrci."statusId" = '1'
    left join "vendorRateContract" vrc on vrc."vendorRateContractId" = vrci."vendorRateContractId" and vrc."statusId" = '1'
    left join cte on cte."materialId" = mm."materialId"
    left join "materialBrands" mb on mb."brandId" = mm."brandId"
    left join "materialCategories" mc on mc."categoryId" = mm."categoryId"
    left join "companies" c on c."companyId" = vrc."companyId" and c."statusId" = '1'
    left join "states" s on s."stateId" = c."stateId"
    where mm."statusId" = '1' and mm."productSpecification" is not null and mm."materialId"::text not ilike '7%';
    """
    df = pd.read_sql(query, conn)
    df.to_json(MAIN_RAW, orient="records", lines=True)

    grouped = {}
    material_json = {}
    with open(MAIN_RAW, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            mid = str(row["materialId"])
            if mid not in grouped:
                grouped[mid] = {
                    "id": mid, "materialId": row["materialId"],
                    "brandName": row.get("brandName"), "categoryName": row.get("categoryName"),
                    "productName": row.get("productName"), "variantName": row.get("variantName"),
                    "productSpecification": row.get("productSpecification"),
                    "productDescription": row.get("productDescription"),
                    "productSpecification_normalized": normalize_productSpecification(row.get("productSpecification", "")),
                    "shortDescription": row.get("shortDescription"), "listPrice": row.get("listPrice"),
                    "companyERPCodes": [], "vendors": [], "ARCvendors": []
                }
                # productSpecificationJSON is kept OUT of the doc that gets
                # uploaded to spec — spec's schema/documents stay exactly
                # as they were. Captured separately here for
                # run_material_attributes() to use afterward.
                material_json[mid] = row.get("productSpecificationJSON")
            if row.get("VRC"):
                vendor = {
                    "VRC": row.get("VRC"), "companyName": row.get("companyName"),
                    "geoMapType": row.get("geoMapType"), "stateName": row.get("stateName"),
                    "vrcListPrice": row.get("vrcListPrice"), "discount": row.get("discount"),
                    "contractPrice": row.get("contractPrice"), "leadTime": row.get("leadTime")
                }
                if vendor not in grouped[mid]["vendors"]:
                    grouped[mid]["vendors"].append(vendor)
            if row.get("ARC"):
                arcVendor = {
                    "ARC": row.get("ARC"), "companyName": row.get("arcCompanyName"),
                    "branchName": row.get("branchName"), "UnitPrice": row.get("unitPrice"),
                    "validityPeriod": row.get("validityPeriod"), "companyERPCode": row.get("companyERPCode"),
                    "arcLeadTime": row.get("arcLeadTime")
                }
                if arcVendor not in grouped[mid]["ARCvendors"]:
                    grouped[mid]["ARCvendors"].append(arcVendor)
            if row.get("companyERPCode"):
                code = row["companyERPCode"]
                if code not in grouped[mid]["companyERPCodes"]:
                    grouped[mid]["companyERPCodes"].append(code)

    with open(MAIN_GROUPED, "w", encoding="utf-8") as f:
        for doc in grouped.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print("grouped materials:", len(grouped))

    upload_file(client, "spec", MAIN_GROUPED, batch_size=5000)
    delete_stale_documents(client, "spec", grouped.keys())
    return grouped, material_json


def run_temp(conn, client):
    print("\n=== TEMP (temp) ===")
    query = """
    with cte as(
      select cerp."materialId", c."companyName", cb."branchName", brc."unitPrice", brc."validityPeriod",
             brc."leadTime",cerp."companyERPCode"
      from "buyerRateContracts" brc
      join "companyERPCodeMap" cerp on brc."companyERPCodeMapId" = cerp."companyERPCodeMapId"
      join "companies" c on c."companyId" = brc."companyId" and c."statusId" = '1'
      left join "companyBranches" cb on cb."branchId" = brc."branchId" and cb."statusId" = '1'
      where brc."statusId" = '1' and cerp."statusId" = '1'
    )
    select mmt."materialId", mb."brandName", mc."categoryName", mmt."productName", mmt."variantName",
    mmt."productSpecification", mmt."productSpecificationJSON", mmt."productDescription", mmt."shortDescription",mmt."listPrice",
    concat(mmt."UOMValue", ' ', mmt."UOM") as "UOM",
    case when cte."materialId" is not null then 'ARC' end as "ARC", cte."companyName" as "arcCompanyName",
    cte."branchName", cte."unitPrice", cte."validityPeriod", cte."companyERPCode", cte."leadTime" as "arcLeadTime"
    from "materialMasterTemp" mmt
    left join cte on cte."materialId" = mmt."materialId"
    left join "materialBrands" mb on mb."brandId" = mmt."brandId"
    left join "materialCategories" mc on mc."categoryId" = mmt."categoryId"
    where mmt."statusId" = '1' and mmt."productSpecification" is not null;
    """
    df = pd.read_sql(query, conn)
    df.to_json(TEMP_RAW, orient="records", lines=True)

    grouped_temp = {}
    temp_material_json = {}
    with open(TEMP_RAW, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            mid = str(row["materialId"])
            if mid not in grouped_temp:
                grouped_temp[mid] = {
                    "id": mid, "materialId": row["materialId"],
                    "brandName": row.get("brandName"), "categoryName": row.get("categoryName"),
                    "productName": row.get("productName"), "variantName": row.get("variantName"),
                    "productSpecification": row.get("productSpecification"),
                    "productSpecification_normalized": normalize_productSpecification(row.get("productSpecification", "")),
                    "productDescription": row.get("productDescription"),
                    "shortDescription": row.get("shortDescription"), "listPrice": row.get("listPrice"),
                    "UOM": row.get("UOM"), "isTemporary": "true",
                    "companyERPCodes": [], "ARCvendors": []
                }
                # Kept OUT of the doc uploaded to temp, same reasoning as
                # run_main — temp's schema/documents stay exactly as they
                # were. Captured separately for run_material_attributes().
                temp_material_json[mid] = row.get("productSpecificationJSON")
            if row.get("ARC"):
                arcVendor = {
                    "ARC": row.get("ARC"), "companyName": row.get("arcCompanyName"),
                    "branchName": row.get("branchName"), "UnitPrice": row.get("unitPrice"),
                    "validityPeriod": row.get("validityPeriod"), "companyERPCode": row.get("companyERPCode"),
                    "arcLeadTime": row.get("arcLeadTime")
                }
                if arcVendor not in grouped_temp[mid]["ARCvendors"]:
                    grouped_temp[mid]["ARCvendors"].append(arcVendor)
            if row.get("companyERPCode"):
                code = row["companyERPCode"]
                if code not in grouped_temp[mid]["companyERPCodes"]:
                    grouped_temp[mid]["companyERPCodes"].append(code)

    with open(TEMP_GROUPED, "w", encoding="utf-8") as f:
        for doc in grouped_temp.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print("grouped temp materials:", len(grouped_temp))

    upload_file(client, "temp", TEMP_GROUPED, batch_size=5000)
    delete_stale_documents(client, "temp", grouped_temp.keys())
    return grouped_temp, temp_material_json


def run_vmi(conn, client):
    print("\n=== VMI (vmi_tags) ===")
    query = """
    select mm."materialId",
           case when "vmiPerm"."permMaterialId" is not null then 'VMI' end as "VMI_perm",
           "vmiPerm"."materialId" as "vmiPerm_linkedMaterialId",
           case when "vmiDirect"."materialId" is not null then 'VMI' end as "VMI_direct",
           "vmiDirect"."permMaterialId" as "vmiDirect_linkedMaterialId"
    from "materialMaster" mm
    left join "VMIContractItems" "vmiPerm" on "vmiPerm"."permMaterialId" = mm."materialId" and "vmiPerm"."statusId" = 1
    left join "VMIContractItems" "vmiDirect" on "vmiDirect"."materialId" = mm."materialId" and "vmiDirect"."statusId" = 1
    where mm."statusId" = '1'
      and (("vmiPerm"."permMaterialId" is not null) or ("vmiDirect"."materialId" is not null));
    """
    df = pd.read_sql(query, conn)
    df.to_json(VMI_RAW, orient="records", lines=True)

    def to_int(val):
        return None if val is None else int(val)

    vmi_docs = {}
    with open(VMI_RAW, "r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            mid = to_int(row.get("materialId"))
            if row.get("VMI_perm"):
                linked = to_int(row.get("vmiPerm_linkedMaterialId"))
                if mid is not None and linked is not None:
                    key = f"{mid}_perm"
                    vmi_docs[key] = {"id": key, "materialId": mid, "linkedMaterialId": linked}
            if row.get("VMI_direct"):
                linked = to_int(row.get("vmiDirect_linkedMaterialId"))
                if mid is not None and linked is not None:
                    key = f"{mid}_direct"
                    vmi_docs[key] = {"id": key, "materialId": mid, "linkedMaterialId": linked}

    with open(VMI_GROUPED, "w", encoding="utf-8") as f:
        for doc in vmi_docs.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print("vmi docs:", len(vmi_docs))

    upload_file(client, "vmi_tags", VMI_GROUPED, batch_size=500)
    delete_stale_documents(client, "vmi_tags", vmi_docs.keys())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print(f"Typesense sync started: {datetime.now():%Y-%m-%d %H:%M:%S}  (date tag: {DATE_TAG})")
    print("=" * 70)

    client = typesense.Client({
        'nodes': [{'host': TYPESENSE_HOST, 'port': '443', 'protocol': 'https'}],
        'api_key': TYPESENSE_API_KEY,
        'connection_timeout_seconds': 300
    })

    conn = None
    try:
        conn = psycopg2.connect(
            host=PG_HOST, database=PG_DATABASE, user=PG_USER,
            password=PG_PASSWORD, port=PG_PORT
        )
        grouped, material_json = run_main(conn, client)
        grouped_temp, temp_material_json = run_temp(conn, client)
        run_vmi(conn, client)
        run_derived_collections(client, grouped, grouped_temp)
        run_material_attributes(client, grouped, material_json, grouped_temp, temp_material_json)
        run_spec_with_attributes(client, grouped, material_json)
    finally:
        if conn is not None:
            conn.close()
            print("\nDB connection closed.")

    cleanup_old_files()
    print(f"\nSync finished OK: {datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Any failure prints a full traceback so the log file captures it.
        print("\n!!! SYNC FAILED !!!")
        traceback.print_exc()
        sys.exit(1)