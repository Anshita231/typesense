"""
attribute_matching.py

DEPLOYMENT NOTE — READ BEFORE EDITING
--------------------------------------
This exact file lives in TWO separate, unlinked folders:
  1. The refresh.py folder (catalog sync) — refresh.py imports this file
     and calls ONLY parse_product_spec_json() (and what it in turn calls:
     canonical_attribute_key(), normalize_attribute_value(),
     ATTRIBUTE_CANONICAL_MAP, extract_pole_count()). These are the
     CATALOG-SIDE functions, everything from the top of the file down
     through "Value helpers".
  2. The search app folder (app.py / search_app.py) — once wired in,
     will call extract_query_attributes() and the four passes under it.
     These are the QUERY-SIDE functions, everything under "Query-side
     extraction" near the bottom of the file.

Since the folders aren't linked, keeping this ONE filename identical in
both places (rather than renaming per-folder) means the import line
(`import attribute_matching`) never has to differ between refresh.py and
the search app — one less thing to keep in sync on top of the file itself.

Whenever this file changes:
  - If the change is in a CATALOG-SIDE function -> copy the updated file
    into the refresh.py folder AND rerun refresh.py.
  - If the change is in a QUERY-SIDE function -> copy the updated file
    into the search app folder AND redeploy the search app. No refresh.py
    rerun needed.
  - If both changed -> update both copies; only rerun refresh.py if the
    catalog-side part actually changed.

Shared module for structured-attribute matching, imported by refresh.py
(index time) and app.py / search_app.py (query time) — one place to fix,
instead of duplicating this logic per file the way parse_query/
find_model_candidate had to be kept in sync by hand across two files
earlier in this project.

WHAT THIS SOLVES
-----------------
Typesense's free-text relevance treats every number in productSpecification
as just another token. It has no concept of "core count must match exactly."
That's why a query for "core 2, 0.5 sqmm" can rank a "core 4" cable, or why
"2.5 sqmm" can match a "25 sqmm" one — textually close, numerically wrong.
This module extracts specific attributes (diameter, core count, cross
section, ...) into their own comparable values, so matching on them can be
exact/near-exact instead of riding on generic text relevance.

CANONICAL MAP
--------------
productSpecificationJSON uses ~1,027 distinct raw "type" strings across the
catalog, many of which are just naming variants of the same real attribute
("DIAMETER" / "HEAD DIAMETER" / "HEAD DIAMETER/SIZE"). ATTRIBUTE_CANONICAL_MAP
collapses those into one canonical key per real attribute.

Every merge below was checked empirically against real data before being
included — specifically, by checking whether the two raw type names ever
CO-OCCUR on the same material. If they do (sometimes at 95%+), they're
almost certainly two different real attributes with similar names, not the
same attribute spelled two ways, and merging them would silently corrupt
matching (e.g. INNER DIAMETER and OUTER DIAMETER co-occur 95.1% of the time
on bearings — merging them would make "22mm bore" match a "22mm OD" bearing).
Some 0%-co-occurrence pairs were still kept separate when their VALUE SHAPES
differ (TEMPERATURE's "-55°C TO +70°C" range vs AMBIENT TEMPERATURE's single
"50 DEGREE CELSIUS" point) — co-occurrence alone doesn't catch that.

This map covers the ~150 highest-volume raw types (currently ~93% of all
attribute occurrences in the catalog). Anything not listed here falls
through to _default_canonical_key(), which just normalizes the raw string
(lowercase, spaces -> underscores) rather than dropping it — long-tail
attributes still get indexed, just without the benefit of merge cleanup.
"""

import re

# ---------------------------------------------------------------------------
# Canonical attribute map
# raw type string (as it appears in productSpecificationJSON, upper-cased)
#   -> canonical key (what gets used as the Typesense field name and the
#      key extraction code should look for)
# ---------------------------------------------------------------------------

ATTRIBUTE_CANONICAL_MAP = {
    # --- diameter family -----------------------------------------------
    # Validated: DIAMETER/HEAD DIAMETER/HEAD DIAMETER-SIZE co-occur ~0%.
    "DIAMETER": "diameter",
    "HEAD DIAMETER": "diameter",
    "HEAD DIAMETER/SIZE": "diameter",

    # Validated: INNER DIAMETER / INSIDE DIAMETER / BORE DIAMETER / BORE
    # all pairwise ~0% co-occurrence.
    "INNER DIAMETER": "inner_diameter",
    "INSIDE DIAMETER": "inner_diameter",
    "BORE DIAMETER": "inner_diameter",
    "BORE": "inner_diameter",

    # Validated: OUTER DIAMETER / OUTSIDE DIAMETER ~0% co-occurrence.
    "OUTER DIAMETER": "outer_diameter",
    "OUTSIDE DIAMETER": "outer_diameter",
    # NOTE: DIAMETER / OUTER DIAMETER also test at ~0% co-occurrence, but
    # OUTER DIAMETER is kept as its own key (not folded into "diameter")
    # because it co-occurs heavily (95.1%) with INNER DIAMETER on bearings
    # — i.e. it's clearly a distinct, specific measurement in that context,
    # not interchangeable with a generic single "diameter" value.

    # NOT merged: THREAD TYPE / THREAD DESIGNATION / THREAD SERIES co-occur
    # 64.5% / 70.0% with each other — genuinely different attributes
    # (coverage vs. standard vs. metric-or-imperial), kept as their own
    # standalone keys via _default_canonical_key().

    # --- length family ---------------------------------------------------
    # Validated: LENGTH / OVERALL LENGTH ~0% co-occurrence.
    "LENGTH": "length",
    "OVERALL LENGTH": "length",
    # NOT merged: THREAD LENGTH has a small but real 0.8% conflict rate
    # with LENGTH (23 materials have both) — kept separate as its own key.

    # --- cable cross-section family --------------------------------------
    # Validated: all four pairwise ~0% co-occurrence.
    "CROSS SECTION AREA": "cross_section",
    "CONDUCTOR SIZE": "cross_section",
    "AREA OF CROSS SECTION": "cross_section",
    "CABLE GAUGE": "cross_section",

    # --- cable core count --------------------------------------------------
    # Validated: ~0% co-occurrence.
    "NUMBER OF CORES": "core_count",
    "CORE OF CABLE": "core_count",

    # --- electrical ratings ------------------------------------------------
    # Validated: CURRENT / CURRENT RATING ~0.2% (negligible).
    "CURRENT": "current_rating",
    "CURRENT RATING": "current_rating",

    # Validated: VOLTAGE / VOLTAGE RATING ~0% co-occurrence.
    "VOLTAGE": "voltage_rating",
    "VOLTAGE RATING": "voltage_rating",
    # NOT merged: INPUT VOLTAGE / OUTPUT VOLTAGE co-occur 99.2% with EACH
    # OTHER (a charger/transformer has both) — each kept as its own key,
    # and neither folded into the general "voltage_rating" bucket, since
    # INPUT VOLTAGE / VOLTAGE tested at ~0% (they'd merge cleanly by that
    # test alone) but doing so would destroy the input/output distinction.
    "INPUT VOLTAGE": "input_voltage",
    "OUTPUT VOLTAGE": "output_voltage",

    "POWER": "power_rating",
    "POWER RATING": "power_rating",
    "POWER RATING (HP/KW)": "power_rating",

    # --- packaging -----------------------------------------------------
    # Validated: ~0% co-occurrence. NOTE: PACK SIZE's values are a mix of
    # quantity ("100 PCS") and container volume ("400 ML") depending on
    # product — value normalization needs to handle both shapes, this
    # merge is just at the naming level.
    "PACK OF": "pack_qty",
    "PACK SIZE": "pack_qty",

    # --- identifiers -------------------------------------------------------
    # Validated: MODEL NO / PART NO / PART CODE all ~0% co-occurrence with
    # each other — consistent with how identifier_match/modelNos already
    # treats these three as interchangeable trigger keywords.
    "MODEL NO": "identifier_code",
    "PART NO": "identifier_code",
    "PART CODE": "identifier_code",
    # Second review pass (prodSpecJsonAll.xlsx, with productName): all
    # confirmed ~0% co-occurrence with MODEL NO — spelling/formatting
    # variants of the same concept, not BRAND MODEL NO / BEARING NO.
    # being distinct attributes the way PART NUMBER was.
    "MODELNO": "identifier_code",
    "MODEL NO.": "identifier_code",
    "BRAND MODEL NO": "identifier_code",
    "BEARING NO.": "identifier_code",
    # NOT merged: PART NUMBER co-occurs with MODEL NO 96.2% of the time —
    # despite the similar name, it's a genuinely separate attribute (looks
    # like an internal/secondary reference number distinct from the
    # manufacturer's model designation). Kept as its own "part_number" key
    # via _default_canonical_key() rather than folded into identifier_code.

    # --- second review pass additions (prodSpecJsonAll.xlsx) ---------------
    # All confirmed ~0% co-occurrence with their target.
    "NO OF PLY": "number_of_ply",
    "NUMBER OF PLY": "number_of_ply",
    "END CONNECTION TYPE": "connection_type",
    "BOX SIZE (L\u00d7W\u00d7H)": "dimensions_lwh",
    "CABLE LENGTH": "length",

    # --- color -----------------------------------------------------------
    # Validated: COLOR / COLOUR / INK COLOR all ~0% co-occurrence.
    "COLOR": "color",
    "COLOUR": "color",
    "INK COLOR": "color",
    # NOT merged: COLOR / COLOR CODE co-occur 100% (a RAL/paint code always
    # sits alongside a plain color name) — COLOR CODE kept as its own key.

    # --- pole count --------------------------------------------------------
    # Validated: POLE / NO OF POLES / NUMBER OF POLE ~0-0.8% co-occurrence.
    # NOTE: POLE's raw values ("3P+N+E") are a compact code, not a bare
    # count — extraction needs to pull the leading digit out, not treat the
    # whole string as the value. See extract_pole_count() below.
    "POLE": "pole_count",
    "NO OF POLES": "pole_count",
    "NUMBER OF POLE": "pole_count",

    # --- misc validated merges ---------------------------------------------
    "DIMENSIONS (LXWXH)": "dimensions_lwh",
    "DIMENSIONS": "dimensions_lwh",

    "GSM": "gsm",
    "PAPER GSM": "gsm",

    "FEATURES": "features",
    "FEATURE": "features",

    # Validated: all four pairwise ~0% co-occurrence — includes a literal
    # typo ("INSUATION") in the raw catalog data, caught because it never
    # co-occurs with the correctly-spelled versions either.
    "INSULATION": "insulation_type",
    "INSULATION TYPE": "insulation_type",
    "TYPE OF INSULATION": "insulation_type",
    "INSUATION": "insulation_type",

    # Validated: all four pairwise ~0% co-occurrence. Values will be
    # heterogeneous (pipe-fitting connections vs. electrical connector
    # types use very different vocab) — this merge avoids missed matches
    # from inconsistent labeling, but exact-value matching on this bucket
    # will be weaker than the cleaner numeric ones above.
    "CONNECTION": "connection_type",
    "CONNECTION TYPE": "connection_type",
    "CONNECTOR TYPE": "connection_type",
    "END CONNECTION": "connection_type",

    "NOMINAL SIZE": "nominal_pipe_size",
    "NOMINAL BORE SIZE": "nominal_pipe_size",
    "PIPE SIZE": "nominal_pipe_size",

    # --- explicitly NOT canonicalized, listed here as documentation --------
    # MATERIAL vs MATERIAL GRADE: 99.4% co-occurrence — composition vs.
    #   strength class, both present together. Never merge.
    # BODY MATERIAL / HEAD MATERIAL / HANDLE MATERIAL / CONDUCTOR MATERIAL /
    #   LUG MATERIAL / INSULATION MATERIAL / WIRE MATERIAL / BRISTLE MATERIAL
    #   / BRAIDED MATERIAL / UPPER MATERIAL / BOX MATERIAL: each names a
    #   different physical PART of an assembly. 0% co-occurrence between
    #   most pairs, but merging would still lose which part a value
    #   describes — kept as their own standalone keys.
    # MAKE: this is brand. Already fully handled by KNOWN_BRANDS matching
    #   in parse_query — does not need a new attribute field at all.
    # SIZE, TYPE: too generic and context-dependent to canonicalize safely
    #   (SIZE means a bolt's thread size, a sheet of paper, or a bottle's
    #   volume depending on product; TYPE is used just as broadly). These
    #   need productAttributes.xlsx-driven category scoping before they're
    #   usable — not a name-collapse job.
    # TEMPERATURE vs AMBIENT TEMPERATURE: 0% co-occurrence (would pass the
    #   usual test), but kept separate because their value SHAPES differ —
    #   one is a range ("-55°C TO +70°C"), the other a single point value.
}


# Every canonical key we've actually reviewed (validated via co-occurrence
# checking, or a deliberate "keep separate" decision) — i.e. every distinct
# value that appears in ATTRIBUTE_CANONICAL_MAP above. This is the ONLY set
# of keys refresh.py is allowed to write as attr_* fields onto spec/temp —
# see KNOWN_ATTRIBUTE_KEYS usage in refresh.py's _add_attribute_fields().
#
# Why this matters: spec/temp have an EXPLICIT Typesense schema (confirmed
# by inspecting it directly — no ".*" catch-all field), which means an
# undeclared field risks the document failing to index. canonical_attribute_key()
# has a fallback (_default_canonical_key) that invents a field name for any
# of the ~800+ unreviewed long-tail raw types — useful for parse_query_json()
# callers that just want the data, but NOT safe to write straight into
# Typesense without the matching field being declared in the schema first.
# Extending this set requires extending the schema at the same time (see
# the schema-update script) — never just adding a canonical map entry alone.
KNOWN_ATTRIBUTE_KEYS = set(ATTRIBUTE_CANONICAL_MAP.values())

# Standalone canonical keys we explicitly reviewed and reasoned about
# during the canonicalization pass, but which never appear as a VALUE in
# ATTRIBUTE_CANONICAL_MAP above — because being reviewed-and-kept-separate
# isn't the same as being a merge target. Without this, high-value
# standalone attributes like "material" (your #2 most common attribute,
# 75K occurrences) would be silently invisible to KNOWN_ATTRIBUTE_KEYS
# even though we deliberately decided to keep them, not because we hadn't
# looked at them.
_REVIEWED_STANDALONE_KEYS = {
    # MATERIAL vs MATERIAL GRADE: 99.4% co-occurrence — composition vs.
    # strength class, confirmed distinct, both kept.
    "material", "material_grade",
    # Each names a different physical PART of an assembly — confirmed
    # distinct in concept even where co-occurrence alone wouldn't catch it.
    "body_material", "head_material", "handle_material", "conductor_material",
    "lug_material", "insulation_material", "wire_material", "bristle_material",
    "braided_material", "box_material", "upper_material",
    # THREAD TYPE / THREAD SERIES / THREAD DESIGNATION: 64.5% / 70.0%
    # co-occurrence with each other — confirmed distinct. THREAD LENGTH:
    # small but real 0.8% conflict with LENGTH, kept as its own key too.
    "thread_type", "thread_series", "thread_designation", "thread_length",
    # PART NUMBER: 96.2% co-occurrence with MODEL NO — confirmed distinct
    # from identifier_code despite the similar name (see ATTRIBUTE_CANONICAL_MAP).
    "part_number",
    # TEMPERATURE vs AMBIENT TEMPERATURE: kept separate due to differing
    # value shape (range vs. point) — categorical only, not numeric, since
    # a range value ("-55C TO +70C") isn't a single comparable number.
    "temperature", "ambient_temperature",
    # The rest of the top-150 review: no merge candidate found, clean
    # single-shape value samples, kept as their own standalone key.
    # Deliberately NOT included, even though reviewed: capacity, grade,
    # jaw_opening, charge_type, suitable_for, additional_information,
    # printed, mod, number_of_pages, package_contents, no_of_pull,
    # bearing_no — each had messy/ambiguous or mixed-unit value samples
    # at review time (e.g. capacity mixed "NA [KG]"/"595 LTR"/"330 ML"),
    # not worth the false-precision of declaring a field for them yet.
    "width", "thickness", "weight", "speed", "phase", "standard",
    "pressure_rating", "series", "cable_diameter", "spanner_size",
    "pipe_schedule", "mounting_type", "belt_type", "belt_series",
    "bolt_type", "origin_type", "nut_qty", "washer_qty", "height", "pitch",
    "volume", "breaking_capacity", "shape", "capacitor_rating",
    "number_of_pins", "drive_size", "grit_size", "load_capacity",
    "tip_size", "shank_size", "frame_size", "tripping_curve", "seal_width",
    "max_rpm", "discharge_capacity", "frame", "container", "battery_capacity",
    "number_of_taps", "fan_diameter", "armouring_type", "capacitance",
    "locking_method", "teeth", "surface_finish", "socket_size", "valve_type",
    "socket_type", "torque", "number_of_blades", "groove_qty", "storage",
    "head_weight", "bristle_size", "working_length", "type_of_ferrule",
    "operating_type",

    # --- second review pass (prodSpecJsonAll.xlsx, with productName) -------
    # Categorical: clean, single-shape value samples, no merge candidate.
    "head_type", "belt_section", "color_code", "packaging_type", "face_type",
    "finish", "tip_type", "end_connection_a", "end_connection_b",
    "compression_type", "type_of_conductor", "fragrance_scent", "grit",
    "bristle_type", "display_type", "flavour", "product_form", "thread_size",
    "gasket_material", "cable_category", "marker_type", "toe_cap",
    "pulley_material",
    # size_a/size_b (pipe fitting reducer/adapter, two ends): values are
    # often fractional ("1/2 [INCHES]"), which the numeric parser doesn't
    # handle correctly (would grab just "1" from "1/2") — kept categorical
    # rather than numeric to avoid silently wrong numbers.
    "size_a", "size_b",
    # Numeric: clean leading-number values, added with NUMERIC_ATTRIBUTE_KEYS
    # extension below.
    "number_of_pages", "ram", "jaw_capacity", "blade_width", "container_size",
    "rpm", "number_of_ply", "number_of_pairs", "wire_diameter", "gauge",
    "quantity", "net_quantity", "number_of_position", "grade_of_filtration",
    "speed_in_rpm", "wheel_diameter", "degree",
}
KNOWN_ATTRIBUTE_KEYS |= _REVIEWED_STANDALONE_KEYS

# identifier_code (MODEL NO / PART NO / PART CODE) is deliberately excluded
# from the Typesense field set — modelNos already covers exactly this data
# as its own dedicated collection. Writing it again as attr_identifier_code
# on spec/temp would just be redundant, unused schema.
KNOWN_ATTRIBUTE_KEYS.discard("identifier_code")

# Numeric-shaped standalone keys added above — extends the original
# NUMERIC_ATTRIBUTE_KEYS set (defined earlier, before normalize_attribute_value)
# with these newly-added ones, so they get a _numeric field too, not just
# a display string. Purely categorical additions (belt_type, standard,
# origin_type, etc.) are deliberately left out of this — they stay
# string-only.
def canonical_attribute_key(raw_type):
    """
    Map a raw productSpecificationJSON "type" string to its canonical
    attribute key. Falls through to a normalized version of the raw string
    for anything not in ATTRIBUTE_CANONICAL_MAP, rather than dropping long-
    tail attributes entirely — coverage without merge cleanup, for types we
    haven't reviewed yet.
    """
    if not raw_type:
        return None
    t = str(raw_type).strip().upper()
    if t in ATTRIBUTE_CANONICAL_MAP:
        return ATTRIBUTE_CANONICAL_MAP[t]
    return _default_canonical_key(t)


def _default_canonical_key(raw_type_upper):
    key = raw_type_upper.strip().lower()
    key = re.sub(r'[^a-z0-9]+', '_', key)
    key = key.strip('_')
    return key or None


# ---------------------------------------------------------------------------
# Parsing productSpecificationJSON (catalog side — used by refresh.py)
# ---------------------------------------------------------------------------

def parse_product_spec_json(raw_json_field):
    """
    Parse one material's productSpecificationJSON value into
    {canonical_key: {"value": ..., "unit": ..., "raw_type": ...}}.

    Handles multiple input shapes, since this field arrives differently
    depending on how it was fetched:
      - A string shaped like a Python list of JSON-encoded dict strings
        ("['{\"type\":...}', ...]") — what pandas produces when exporting
        a JSONB column to Excel (confirmed from prodSpec.xlsx).
      - A string that's directly a JSON array (a plain json.loads away).
      - A native Python list already — confirmed happening when fetched
        LIVE via pd.read_sql, where psycopg2 can auto-deserialize a JSONB
        column into a real list before pandas ever sees a string. Each
        item in that list might be a dict already, or still a JSON string.
    Every shape converges to the same per-item dict-parsing logic below.

    Returns {} on anything unparseable rather than raising, since refresh.py
    syncs hundreds of thousands of rows and one bad record shouldn't take
    down the batch.
    """
    if not raw_json_field:
        return {}

    import ast
    import json as _json

    items = raw_json_field
    if isinstance(items, str):
        try:
            items = ast.literal_eval(items)
        except Exception:
            try:
                items = _json.loads(items)
            except Exception:
                return {}

    if not isinstance(items, (list, tuple)):
        return {}

    result = {}
    for item in items:
        attr = item
        if isinstance(item, str):
            try:
                attr = _json.loads(item)
            except Exception:
                continue
        if not isinstance(attr, dict):
            continue

        raw_type = attr.get("type")
        value = attr.get("value")
        if raw_type is None or value is None:
            continue

        key = canonical_attribute_key(raw_type)
        if not key:
            continue

        # Don't overwrite an already-populated canonical key with a second
        # raw type that mapped to the same bucket on this material — first
        # one wins. (Two raw types mapping to the same canonical key on the
        # SAME material would mean the co-occurrence check should have
        # caught a conflict; this is a defensive fallback, not expected to
        # fire on the validated merges above.)
        if key in result:
            continue

        result[key] = {
            "value": str(value).strip(),
            "unit": attr.get("unit"),
            "raw_type": str(raw_type).strip(),
        }

    # Normalize every extracted value now that we know its canonical key —
    # this needs the whole result dict assembled first for POLE, which
    # reads from "pole_count"'s own raw value after canonicalization.
    for key, entry in result.items():
        normalized = normalize_attribute_value(key, entry["value"], entry.get("unit"))
        entry["normalized"] = normalized

    return result


# ---------------------------------------------------------------------------
# Value normalization
#
# Every canonical key falls into one of two shapes:
#   - numeric-ish (diameter, length, cross_section, current_rating, ...):
#     needs a comparable number + unit pulled out of a messy string.
#   - categorical (material, color, type, insulation_type, ...): just needs
#     consistent casing for exact-match comparison.
#
# Built from the real value samples pulled from prodSpec.xlsx while
# designing the canonical map — not guessed ahead of the data. Known
# simplification: range values ("180 - 250 AMP") only capture their FIRST
# number for now; full range-aware matching (query says "200A", catalog
# says "180-250A" should match) is a follow-up, not built here yet.
# ---------------------------------------------------------------------------

NUMERIC_ATTRIBUTE_KEYS = {
    "diameter", "inner_diameter", "outer_diameter", "length", "thread_length",
    "cross_section", "core_count", "current_rating", "voltage_rating",
    "input_voltage", "output_voltage", "power_rating", "width", "thickness",
    "pack_qty", "gsm", "pole_count", "weight", "height", "dimensions_lwh",
    "nominal_pipe_size",
}
# Extends the base set above with the numeric-shaped keys from
# _REVIEWED_STANDALONE_KEYS (defined earlier, near KNOWN_ATTRIBUTE_KEYS) —
# split into two places because this needs to exist before
# normalize_attribute_value() below can reference it, while
# _REVIEWED_STANDALONE_KEYS lives with the rest of the key/field
# documentation earlier in the file. Purely categorical additions
# (belt_type, standard, origin_type, etc.) are deliberately left out —
# they stay string-only.
NUMERIC_ATTRIBUTE_KEYS |= {
    "pitch", "volume", "breaking_capacity", "capacitor_rating",
    "number_of_pins", "drive_size", "grit_size", "load_capacity",
    "tip_size", "shank_size", "frame_size", "seal_width", "max_rpm",
    "discharge_capacity", "battery_capacity", "number_of_taps",
    "fan_diameter", "capacitance", "teeth", "socket_size", "torque",
    "number_of_blades", "groove_qty", "head_weight", "bristle_size",
    "working_length", "nut_qty", "washer_qty", "speed", "pressure_rating",
    # second review pass
    "number_of_pages", "ram", "jaw_capacity", "blade_width",
    "container_size", "rpm", "number_of_ply", "number_of_pairs",
    "wire_diameter", "gauge", "quantity", "net_quantity",
    "number_of_position", "grade_of_filtration", "speed_in_rpm",
    "wheel_diameter", "degree",
}

_BLANK_VALUES = {"NA", "N/A", "NULL", "NONE", "NOT AVAILABLE", "-", ""}

# "M12", "M 12", "M-12", and the dirty "M6 MM" (redundant trailing unit
# seen in the real data) — a metric thread designation is kept as its own
# token ("M12"), not decomposed into a bare 12mm measurement, since the
# catalog value IS the whole "M12" string.
_M_PREFIX_RE = re.compile(r'^\s*M\s*-?\s*(\d+(?:\.\d+)?)\s*(?:MM)?\s*$', re.IGNORECASE)

# A plain number, optionally with a unit trailing it in the same string
# ("50 MM", "0.5SQMM") when no separate "unit" field was provided.
_LEADING_NUMBER_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)')


def normalize_attribute_value(canonical_key, raw_value, raw_unit=None):
    """
    Returns None for blank/unparseable-as-meaningful values, otherwise:
        {"display": <cleaned string for exact-match/display>,
         "numeric": <float or None>,
         "unit": <cleaned unit string or None>,
         "is_metric_thread": <bool>}
    """
    if raw_value is None:
        return None
    value_str = str(raw_value).strip()
    if not value_str or value_str.upper() in _BLANK_VALUES:
        return None

    if canonical_key == "pole_count":
        count = extract_pole_count(value_str)
        if count is None:
            return None
        return {"display": count, "numeric": float(count), "unit": None, "is_metric_thread": False}

    if canonical_key not in NUMERIC_ATTRIBUTE_KEYS:
        # Categorical attribute — consistent casing for exact-match
        # comparison, nothing numeric to extract.
        return {"display": value_str.upper(), "numeric": None, "unit": None, "is_metric_thread": False}

    m_match = _M_PREFIX_RE.match(value_str)
    if m_match:
        return {
            "display": f"M{m_match.group(1)}",
            "numeric": float(m_match.group(1)),
            "unit": "MM",
            "is_metric_thread": True,
        }

    num_match = _LEADING_NUMBER_RE.match(value_str)
    if num_match:
        numeric = float(num_match.group(1))
        # unit comes from the explicit "unit" JSON field if present,
        # otherwise whatever text trails the number in the value itself
        # ("50 MM" with no separate unit field).
        unit = raw_unit or value_str[num_match.end():].strip(" []")
        unit = unit.strip().upper() if unit else None
        return {
            "display": num_match.group(1),
            "numeric": numeric,
            "unit": unit,
            "is_metric_thread": False,
        }

    # Couldn't parse a number out of it (e.g. "NOT AVAILABLE", a free-text
    # value in a field we expected to be numeric) — keep the cleaned
    # string so it's still visible/searchable, just not numerically
    # comparable.
    return {"display": value_str.upper(), "numeric": None, "unit": None, "is_metric_thread": False}


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def extract_pole_count(raw_value):
    """
    POLE's raw values are compact codes like "3P+N+E", not a bare count.
    Pull the leading digit out for numeric comparison; NUMBER OF POLE / NO
    OF POLES already come as plain numbers and pass through unchanged.
    """
    if not raw_value:
        return None
    m = re.match(r'\s*(\d+)', str(raw_value))
    return m.group(1) if m else None


# ===========================================================================
# EVERYTHING ABOVE THIS LINE = CATALOG-SIDE (used by refresh.py)
# EVERYTHING BELOW THIS LINE = QUERY-SIDE (used by the search app, once wired)
# ===========================================================================

# ---------------------------------------------------------------------------
# Query-side extraction — the harder half. The catalog side has structured
# JSON already; a buyer's query is free text, sometimes labeled ("DIAMETER
# M12, LENGTH 100 MM") but mostly not ("Allen Bolts M6*50", "2.5 sq mm
# single core").
#
# Three passes, in priority order — a later pass only fills a canonical key
# the earlier ones didn't already find, so an explicit label always wins
# over a guessed unit, which wins over the M-prefix guess:
#   1. Labeled  — "LABEL VALUE" pairs, comma/semicolon separated. Covers
#      both the small pre-labeled query population AND natural phrases
#      embedded in longer text ("Allen Bolt, Diameter 12mm, Length 50mm").
#   2. Unit-anchored — a number glued/adjacent to a unit that unambiguously
#      identifies ONE attribute ("0.5sqmm" -> cross_section, "2 core" ->
#      core_count) — no label needed, the unit itself disambiguates. This
#      is the direct fix for the "core 2 0.5sqmm" -> "core 4" failure mode.
#   3. Metric thread — "M12" -> diameter (a bolt thread designation).
# ---------------------------------------------------------------------------

# Label words a query might use for a given canonical key — built from the
# raw JSON type names already in ATTRIBUTE_CANONICAL_MAP (a query label is
# likely to use the same words the catalog does), plus natural-language
# extras a buyer might type that never appear as a raw JSON "type".
#
# Deliberately does NOT include bare "core"/"cores"/"sqmm"/"sq mm"/"sq.mm"
# here — those are ALSO unit words (UNIT_TO_CANONICAL below), and a bare
# unit word with no number in front of it ("single core", or just "core"
# inside an unrelated phrase) isn't a real "LABEL VALUE" construction.
# Confirmed by testing: allowing "core" as a standalone label made
# "single core - Flexible" match "core" as a label and swallow "- Flexible"
# as its "value", producing a nonsense attr_core_count. Multi-word phrases
# ("cross section", "core of cable") stay in, since they're unambiguous —
# nothing else in a query would coincidentally contain that exact phrase.
_LABEL_TO_CANONICAL = {raw.lower(): canon for raw, canon in ATTRIBUTE_CANONICAL_MAP.items()}
_LABEL_TO_CANONICAL.update({
    "dia": "diameter",
    "diameter": "diameter",
    "length": "length",
    "long": "length",
    "no of core": "core_count",
    "no of cores": "core_count",
    "cross section": "cross_section",
    "current": "current_rating",
    "voltage": "voltage_rating",
    "material": "material",
    "colour": "color",
    "color": "color",
    "width": "width",
    "thickness": "thickness",
    "weight": "weight",
})
# Longest labels first, so "head diameter" matches before the shorter
# "diameter" would swallow part of it.
_LABEL_KEYS_BY_LENGTH = sorted(_LABEL_TO_CANONICAL.keys(), key=len, reverse=True)

# A unit that, on its own, unambiguously identifies one canonical
# attribute — no label needed. Deliberately does NOT include "mm", since
# that's used for diameter/length/width/thickness depending on context;
# ambiguous units are handled via the AxB/labeled paths instead, not guessed
# here.
UNIT_TO_CANONICAL = {
    "sqmm": "cross_section", "sq.mm": "cross_section",
    "core": "core_count", "cores": "core_count",
    "v": "voltage_rating", "volt": "voltage_rating", "volts": "voltage_rating",
    "vac": "voltage_rating", "vdc": "voltage_rating",
    "a": "current_rating", "amp": "current_rating", "amps": "current_rating",
    "ampere": "current_rating", "amperes": "current_rating",
    "w": "power_rating", "watt": "power_rating", "watts": "power_rating",
    "kg": "weight", "gm": "weight", "gms": "weight", "grm": "weight",
    "gram": "weight", "grams": "weight",
}

_LABELED_VALUE_RE_CACHE = {}


def _labeled_value_regex(label):
    if label not in _LABELED_VALUE_RE_CACHE:
        # value = whatever follows the label (and an optional : or -)
        # up to the next comma/semicolon or end of string.
        _LABELED_VALUE_RE_CACHE[label] = re.compile(
            rf'\b{re.escape(label)}\b\s*[:\-]?\s*([^,;]+)', re.IGNORECASE
        )
    return _LABELED_VALUE_RE_CACHE[label]


def extract_labeled_attributes(query_text):
    """
    Pass 1: explicit 'LABEL VALUE' mentions, comma/semicolon separated.

    Only fires when the label word is NOT immediately preceded by a
    number — "DIAMETER M12" is label-then-value, but "0.5 SQMM" is
    value-then-unit (SQMM is also a recognized label/unit word, since a
    labeled query could say "CROSS SECTION 0.5 SQMM"). Without this check,
    "2.5 sq mm single core" would match "sq mm" as a label and swallow
    "single core" as its "value" — confirmed by testing against real
    queries before this guard was added.
    """
    if not query_text:
        return {}

    found = {}
    for label in _LABEL_KEYS_BY_LENGTH:
        canon = _LABEL_TO_CANONICAL[label]
        if canon in found:
            continue
        m = _labeled_value_regex(label).search(query_text)
        if not m:
            continue

        preceding = query_text[:m.start()].rstrip()
        if preceding and preceding[-1].isdigit():
            continue  # this occurrence is a unit-after-number, not a label

        value = m.group(1).strip()
        if not value:
            continue
        normalized = normalize_attribute_value(canon, value)
        if normalized:
            found[canon] = normalized
    return found


# Spelling/spacing variants collapsed to one recognized form before the
# unit-anchored regex runs, so "10 sq.mm", "10 sq mm", "10sqmm" all match
# the same way instead of only the glued form.
_UNIT_SPELLING_VARIANTS = {
    r'sq\s*\.?\s*mm': 'sqmm',
    r'mm\s*2\b': 'sqmm',
}


def _normalize_unit_spellings(text):
    for pattern, replacement in _UNIT_SPELLING_VARIANTS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


_UNIT_ANCHORED_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*[-]?\s*([a-zA-Z]+)'
)


def extract_unit_anchored_attributes(query_text):
    """Pass 2: number+unit pairs where the unit alone identifies the attribute."""
    if not query_text:
        return {}

    text = _normalize_unit_spellings(query_text)
    found = {}
    for num_str, unit_str in _UNIT_ANCHORED_RE.findall(text):
        canon = UNIT_TO_CANONICAL.get(unit_str.lower())
        if not canon or canon in found:
            continue
        normalized = normalize_attribute_value(canon, num_str, unit_str)
        if normalized:
            found[canon] = normalized
    return found


_METRIC_THREAD_AXB_RE = re.compile(
    r'\bM\s*-?\s*(\d+(?:\.\d+)?)\s*[xX\*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|inch|in)?\b',
    re.IGNORECASE
)
_METRIC_THREAD_RE = re.compile(r'\bM\s*-?\s*(\d+(?:\.\d+)?)\b', re.IGNORECASE)


def extract_metric_thread_attributes(query_text):
    """
    Pass 3: 'M12' / 'M16x150' style metric thread designation -> diameter
    (and length, for the AxB-glued form).

    Checks the glued-AxB variant first: "M16x150" has no word boundary
    between "16" and the "x" that follows it (both are word characters),
    so the plain _METRIC_THREAD_RE below can never match it — same shape
    bug as the "M4X25MM" fix already made in search_app.py. Falls back to
    bare "M12" (no trailing xN) otherwise.
    """
    if not query_text:
        return {}

    found = {}
    m_axb = _METRIC_THREAD_AXB_RE.search(query_text)
    if m_axb:
        diam = normalize_attribute_value("diameter", f"M{m_axb.group(1)}")
        if diam:
            found["diameter"] = diam
        length = normalize_attribute_value("length", m_axb.group(2), m_axb.group(3) or "MM")
        if length:
            found["length"] = length
        return found

    m = _METRIC_THREAD_RE.search(query_text)
    if m:
        diam = normalize_attribute_value("diameter", f"M{m.group(1)}")
        if diam:
            found["diameter"] = diam
    return found


# "20 X 150 MM" — a shared trailing unit covering BOTH numbers in an AxB
# dimension pattern, the same shape app.py/search_app.py already parse for
# general search. Bolt convention (confirmed against real labeled queries
# during design): first number = diameter, second = length. Kept as its
# own pass here (not reused from search_app.py directly) since this module
# needs to stay importable standalone by refresh.py, without a dependency
# on the Flask app's request-handling code.
_AXB_SHARED_UNIT_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*[xX\*]\s*(\d+(?:\.\d+)?)\s*(mm|cm|inch|in)?\b'
)


def extract_axb_dimension_attributes(query_text):
    """Pass 4: 'AxB' dimension pattern -> diameter (first) + length (second)."""
    if not query_text:
        return {}
    m = _AXB_SHARED_UNIT_RE.search(query_text)
    if not m:
        return {}
    d1, d2, unit = m.group(1), m.group(2), (m.group(3) or "MM")
    found = {}
    diam = normalize_attribute_value("diameter", d1, unit)
    if diam:
        found["diameter"] = diam
    length = normalize_attribute_value("length", d2, unit)
    if length:
        found["length"] = length
    return found


def extract_cable_axb_attributes(query_text, already_found):
    """
    When core_count is already known (from another pass) and there's an
    'AxB' pair whose second number matches that core count, the first
    number is the cross-section — it just didn't carry its own "sqmm" unit
    ("1.5 X 3 CORE" -> cross_section=1.5, confirmed by the "3" matching the
    already-found core_count of 3, not guessed positionally).
    """
    if "core_count" not in already_found or "cross_section" in already_found:
        return {}
    core_val = already_found["core_count"].get("numeric")
    if core_val is None:
        return {}
    m = _AXB_SHARED_UNIT_RE.search(query_text)
    if not m:
        return {}
    d1, d2 = m.group(1), m.group(2)
    try:
        if float(d2) == core_val:
            normalized = normalize_attribute_value("cross_section", d1, "SQMM")
            return {"cross_section": normalized} if normalized else {}
    except ValueError:
        pass
    return {}


def extract_query_attributes(query_text):
    """
    Run all four passes and merge, in priority order (labeled > unit-
    anchored > metric-thread > AxB dimension). Returns
    {canonical_key: normalized_value}, same shape as
    parse_product_spec_json's per-attribute "normalized" entry.
    """
    if not query_text:
        return {}

    result = {}
    for pass_fn in (extract_labeled_attributes, extract_unit_anchored_attributes,
                     extract_metric_thread_attributes):
        for canon, normalized in pass_fn(query_text).items():
            if canon not in result:
                result[canon] = normalized

    # The AxB pass assumes bolt convention (first=diameter, second=length)
    # for an unlabeled "NxM" pattern — but the same shape means something
    # different for other product types ("1.5 X 3" on a cable = cross
    # section x core count, not diameter x length). Without product-
    # category detection, skip it whenever an earlier pass already found a
    # cable-specific signal — that's strong evidence this isn't a bolt,
    # and applying the bolt convention anyway would inject wrong values
    # (confirmed on "CABLE 1.5 X 3 CORE" during testing: skipping this
    # guard produced a false diameter/length pair alongside the correct
    # core_count).
    if "core_count" not in result and "cross_section" not in result:
        for canon, normalized in extract_axb_dimension_attributes(query_text).items():
            if canon not in result:
                result[canon] = normalized
    else:
        for canon, normalized in extract_cable_axb_attributes(query_text, result).items():
            if canon not in result:
                result[canon] = normalized
    return result