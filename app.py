from flask import Flask, render_template, request, jsonify
import typesense
import pandas as pd
import re
import os
from dotenv import load_dotenv
import attribute_matching
load_dotenv()

DOUBLE_SYNONYMS = {
    "t bolt": "t-bolt",
    "u clamp": "u-clamp",
    "o ring": "o-ring",
    "pliers" : "plier",
}

SINGLE_SYNONYMS = {
    "mm2" : "sqmm",
    "cores": "core",
    "cables":"cable",
    "SQ" : "sqmm",
    "flex " : "flexible ",
    "single" : "1",
    "one" : "1",
    "-": " ",
    "screw driver": "screwdriver",
    "zz": "2 z",
    "core of cable":"number of cores",
    " cu ":" copper ",
    "hexagonal ": "hex ",
    "v belt": "v-belt",
    "belt v": "v-belt",
    "allen bolt" : "cap screw"
}

def expand_query(q):
    q = q.lower()
    variants = [q]
    v5 = re.sub(r'\b([a-ln-z]+)-(\d+)\b', r'\1\2', q)
    v6 = re.sub(r'\b([a-ln-z]+)-(\d+)\b', r'\1 \2', q)
    v7 = re.sub(r'\b([a-ln-z]+)\s+(\d+)\b', r'\1\2', q)
    v8 = re.sub(r'\b([a-ln-z]+)\s+(\d+)\b', r'\1-\2', q)
    v9 = re.sub(r'\b([a-ln-z]+)(\d+)\b', r'\1 \2', q)
    v10 = re.sub(r'\b([a-ln-z]+)(\d+)\b', r'\1-\2', q)
    v11 = re.sub(r'\bm\s+(\d+)\b', r'\1', q)
    for v in [v5, v6, v7, v8, v9, v10, v11]:
        if v not in variants:
            variants.append(v)
    current_variants = variants.copy()

    for q2 in current_variants:
        for k, v in DOUBLE_SYNONYMS.items():

            if k in q2:
                new_q = q2.replace(k, v)
                if new_q not in variants:
                    variants.append(new_q)

            if v in q2:
                new_q = q2.replace(v, k)
                if new_q not in variants:
                    variants.append(new_q)

    for q2 in current_variants:
        for k, v in SINGLE_SYNONYMS.items():

            if k in q2:
                new_q = q2.replace(k, v)
                if new_q not in variants:
                    variants.append(new_q)

    normalized = []
    for v in variants:
        nv = " ".join(v.split())
        if nv and nv not in normalized:
            normalized.append(nv)
    return normalized

SEPARATE_NUMBER_WORDS = {
    "core", "sqmm", "sq", "mm", "mm2", "inch", "pin", "amp", "kv", "v", "w", "ltr", "kg", "gm", "m"}

WORDS_PATTERN = "|".join(map(re.escape, SEPARATE_NUMBER_WORDS))

def spaced_code_variant(text):
    return re.sub(r'(?<=[a-zA-Z])(?=\d)|(?<=\d)(?=[a-zA-Z])', ' ', text)

def separate_number_words(text):
    text = re.sub(
        rf'(\d+(?:\.\d+)?)(?=({WORDS_PATTERN})\b)',
        r'\1 ',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        rf'\b({WORDS_PATTERN})(?=\d)',
        r'\1 ',
        text,
        flags=re.IGNORECASE
    )
    return text

def is_temporary(product):
    val = product.get("isTemporary", False)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)

def sort_priority(product):
    has_vrc = len(product.get("vendors", [])) > 0
    has_arc = len(product.get("ARCvendors", [])) > 0

    if has_vrc:
        vendor_rank = 0
    elif has_arc:
        vendor_rank = 1
    else:
        vendor_rank = 2
    return (1 if is_temporary(product) else 0, vendor_rank)

def erp_sort_priority(product):
    has_vrc = len(product.get("vendors", [])) > 0
    has_arc = len(product.get("ARCvendors", [])) > 0

    if has_vrc:
        vendor_rank = 0
    elif has_arc:
        vendor_rank = 1
    else:
        vendor_rank = 2
    return (vendor_rank, 1 if is_temporary(product) else 0)

def soft_demote(items, conflicting_ids, keep_within=15):
    if not conflicting_ids:
        return items
    clean = [o for o in items if str(o.get('MaterialId')) not in conflicting_ids]
    dirty = [o for o in items if str(o.get('MaterialId')) in conflicting_ids]
    if not dirty:
        return items
    cut = min(keep_within, len(clean))
    return clean[:cut] + dirty + clean[cut:]

def attach_vmi_tags(output, client):
    if not output:
        return output

    ids = list({str(o["MaterialId"]) for o in output if o.get("MaterialId")})
    if not ids:
        return output

    try:
        vmi_hits = client.collections["vmi_tags"].documents.search({
            "q": "*",
            "filter_by": f"materialId:=[{','.join(ids)}]",
            "per_page": 250
        })

        vmi_map = {}
        for hit in vmi_hits["hits"]:
            doc = hit["document"]
            vmi_map.setdefault(doc["materialId"], []).append(doc["linkedMaterialId"])

        for o in output:
            linked = vmi_map.get(o["MaterialId"])
            if linked:
                o["VMI"] = linked

    except Exception:
        pass

    return output

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

brands_df = pd.read_excel(
    os.path.join(BASE_DIR, "data", "Brands.xlsx")
)

KNOWN_BRANDS = {
    str(x).strip().lower()
    for x in brands_df.iloc[:, 0].dropna()
}

attributes_df = pd.read_excel(
    os.path.join(BASE_DIR, "data", "Attributes.xlsx")
)
KNOWN_ATTRIBUTES = {
    str(x).strip().lower()
    for x in attributes_df.iloc[:, 0].dropna()
}

def parse_query(query):

    query_lower = query.lower()

    material_id = None
    temp_material_id = None
    identifier_number = None

    if query_lower.isdigit():
        num = int(query_lower)
        if 100000 <= num <= 800000:
            material_id = query_lower
        if len(query_lower) == 7:
            temp_material_id = query_lower

    identifier_match = re.search(
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
        query_lower,
        flags=re.IGNORECASE
    )

    if identifier_match:
        keyword_part = identifier_match.group(0)[:-len(identifier_match.group(1))] if identifier_match.group(1) else identifier_match.group(0)
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

        if id_tokens:
            identifier_number = " ".join(id_tokens)
            matched_text = keyword_part + " ".join(id_tokens)
            query_lower = query_lower.replace(matched_text, " ", 1)

    words = query_lower.split()

    brand = None
    attributes = []
    remaining = []

    for word in words:
        if word in KNOWN_BRANDS:
            brand = word
        else:
            remaining.append(word)
            if word in KNOWN_ATTRIBUTES:
                attributes.append(word)

    return {
        "material_id": material_id,
        "temp_material_id": temp_material_id,
        "identifier_number": identifier_number,
        "brand": brand,
        "attributes": attributes,
        "query": " ".join(remaining)
    }


GENERIC_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "of", "with", "in", "on", "at", "to", "by",
    "please", "need", "want", "buy", "price", "rate", "cost", "available",
    "item", "product", "piece", "pcs", "pc", "qty", "quantity", "required",
    "urgent", "new", "old", "no", "number", "model", "part", "code", "type", "size",
    "make", "makes",
}

def find_model_candidate(query_text):
    if not query_text or not query_text.strip():
        return None

    text = separate_number_words(query_text.lower())
    tokens = text.split()

    skip_words = KNOWN_BRANDS | KNOWN_ATTRIBUTES | SEPARATE_NUMBER_WORDS | GENERIC_STOPWORDS | {"cores"}
    BEARING_SUFFIX_HINTS = {"zz", "rs", "rz", "du", "vv", "nr", "llu", "llb", "2z", "z"}

    def is_suffix_like(tok):
        clean = tok.strip(".,")
        return any(ch.isdigit() for ch in clean) or clean in BEARING_SUFFIX_HINTS

    drop = set()
    for i, tok in enumerate(tokens):
        clean = tok.strip(".,")
        if clean in skip_words:
            drop.add(i)

            for j in (i - 1, i + 1):
                if 0 <= j < len(tokens) and re.fullmatch(r'\d+(\.\d+)?', tokens[j]):
                    drop.add(j)
        elif re.fullmatch(r'\d+(\.\d+)?', clean):

            next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
            if next_tok and next_tok.strip(".,") not in skip_words and is_suffix_like(next_tok):
                continue
            drop.add(i)

    remaining_idx = [i for i in range(len(tokens)) if i not in drop]

    def looks_like_code(tok):
        return (
            (re.search(r'[a-z]', tok) and re.search(r'\d', tok))
            or (re.search(r'[a-z0-9][\-/][a-z0-9]', tok) and re.search(r'\d', tok))
        )

    pure_num_range = re.compile(r'^\d+(\.\d+)?[\-/]\d+(\.\d+)?$')
    unit_words = SEPARATE_NUMBER_WORDS | KNOWN_ATTRIBUTES | {"cores"}

    qualifying = []
    skip_next = False
    for pos, i in enumerate(remaining_idx):
        if skip_next:
            skip_next = False
            continue
        tok = tokens[i]

        clean_num = tok.strip(".,")
        if re.fullmatch(r'\d{3,}', clean_num) and i + 1 < len(tokens):
            next_tok = tokens[i + 1]
            next_clean = next_tok.strip(".,")
            if next_clean not in skip_words and is_suffix_like(next_tok):
                qualifying.append(tok.strip(",;").lstrip("-"))
                qualifying.append(next_clean.strip(",;").lstrip("-"))
                if pos + 1 < len(remaining_idx) and remaining_idx[pos + 1] == i + 1:
                    skip_next = True
                continue

        if not looks_like_code(tok):
            continue
        if pure_num_range.fullmatch(tok):
            neighbours = [tokens[j] for j in (i - 1, i + 1) if 0 <= j < len(tokens)]
            if any(n.strip(".,") in unit_words for n in neighbours):
                continue
        qualifying.append(tok.strip(",;").lstrip("-"))

    if qualifying:
        candidate = " ".join(qualifying)
        bare = candidate.replace(" ", "").replace("-", "").replace("/", "")
        if 2 <= len(bare) <= 20:
            return candidate
    return None

client = typesense.Client({
    'nodes': [{
        'host': os.getenv('TYPESENSE_HOST'),
        'port': '443',
        'protocol': 'https'
    }],
    'api_key': os.getenv('TYPESENSE_API_KEY'),
    'connection_timeout_seconds': 20
})

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/search")

def search():
    query = request.args.get("q", "")
    query = query.lower()
    q2 = query
    query = query.replace(",", " ")
    query = query.replace("mm2", "sqmm")
    query = query.replace("(", " ")
    query = query.replace(")", " ")
    query = query.replace(":", " ")
    query = query.replace("dowell ", "dowells ")
    query = re.sub(r'\bbrands?\b', ' ', query)
    query = " ".join(query.split())
    parsed = parse_query(query)
    _m_thread_stash = []

    def _stash_m_thread(m):
        if m.group(2):
            token = f"m{m.group(1)}x{m.group(2)}"
        else:
            token = f"m{m.group(1)}"
        _m_thread_stash.append(token)
        return f"mthreadstash{len(_m_thread_stash) - 1}x"

    query = re.sub(
        r'\bm\s*(\d+(?:\.\d+)?)(?:\s*[xX\*]\s*(\d+(?:\.\d+)?))?',
        _stash_m_thread,
        query,
        flags=re.IGNORECASE
    )

    query = separate_number_words(query)

    for _i, _token in enumerate(_m_thread_stash):
        query = query.replace(f"mthreadstash{_i}x", _token)

    query = re.sub(
        rf'(\d+(?:\.\d+)?)(?=({WORDS_PATTERN})\b)',
        r'\1 ',
        query,
        flags=re.IGNORECASE
    )
    glued_prefix_match = re.search(r'\b([a-z])(\d+(?:\.\d+)?)\s*[xX\*]\s*(\d+(?:\.\d+)?)\b', query, flags=re.IGNORECASE)
    glued_prefix_letter_num = (glued_prefix_match.group(1) + glued_prefix_match.group(2)) if glued_prefix_match else None

    k = 0
    dimension_variants = []

    is_dimension_range = bool(re.search(
        r'\b\d+(?:\.\d+)?\s*[xX\*]\s*\d+(?:\.\d+)?\s*(?:~|-|to)\s*\d+(?:\.\d+)?\s*[xX\*]\s*\d+(?:\.\d+)?\b',
        query, flags=re.IGNORECASE
    ))

    if is_dimension_range:
        pass

    elif re.search(r'(\d+(?:-\d+)?/\d+)"?\s*[xX\*]\s*(\d+(?:-\d+)?(?:/\d+)?)"?', query):
        m_frac = re.search(r'(\d+(?:-\d+)?/\d+)"?\s*[xX\*]\s*(\d+(?:-\d+)?(?:/\d+)?)"?', query)
        k = 1
        d1 = m_frac.group(1)
        d2 = m_frac.group(2)
        dimension_variants.append(
            re.sub(r'(\d+(?:-\d+)?(?:/\d+)?)"?\s*[xX\*]\s*(\d+(?:-\d+)?(?:/\d+)?)"?',
                f'{d1} inch diameter {d2} inch length', query)
        )

    elif glued_prefix_match:
        k = 1
        d2 = glued_prefix_match.group(3)
        dimension_variants.append(
            re.sub(r'\b[a-z]\d+(?:\.\d+)?\s*[xX\*]\s*\d+(?:\.\d+)?\b',
                   f'{glued_prefix_letter_num} length {d2} diameter', query, flags=re.IGNORECASE)
        )
        dimension_variants.append(
            re.sub(r'\b[a-z]\d+(?:\.\d+)?\s*[xX\*]\s*\d+(?:\.\d+)?\b',
                   f'{glued_prefix_letter_num} size {d2} length', query, flags=re.IGNORECASE)
        )
    else:
        m = re.search(r'\b(\d+)\s*[xX\*]\s*(\d+)\b', query)
        if m:
            k = 1
            d1 = m.group(1)
            d2 = m.group(2)
            dimension_variants.append(
                re.sub(r'\b\d+\s*[xX\*]\s*\d+\b', f'{d1} length {d2} diameter', query)
            )
            dimension_variants.append(
                re.sub(r'\b\d+\s*[xX\*]\s*\d+\b', f'{d1} size {d2} length', query)
            )

    parsed1 = parse_query(query)
    material_id = parsed["material_id"]
    temp_material_id = parsed.get("temp_material_id")
    identifier_number = parsed['identifier_number']
    brand = parsed['brand']
    attributes = parsed['attributes']
    clean_query = parsed1['query']

    model_candidate = None
    if not identifier_number:
        model_candidate = find_model_candidate(clean_query)

    known_model_results = []
    _THREAD_LIKE_MODEL_NO = re.compile(r'^m\d{1,3}$', re.IGNORECASE)
    _MODELNO_SEARCH_PER_PAGE = 50
    try:
        modelno_base_text = identifier_number if identifier_number else query

        _code_sep_re = re.compile(r'\b([a-ln-z]+)[\s\-]+(\d+)\b', re.IGNORECASE)
        modelno_query_variants = [modelno_base_text]
        for v in (
            _code_sep_re.sub(r'\1\2', modelno_base_text),
            _code_sep_re.sub(r'\1 \2', modelno_base_text),
            _code_sep_re.sub(r'\1-\2', modelno_base_text),
            spaced_code_variant(modelno_base_text),
            re.sub(r'\b([a-ln-z]+)(\d+)\b', r'\1-\2', modelno_base_text),
        ):
            if v and v not in modelno_query_variants:
                modelno_query_variants.append(v)

        _tokens = modelno_base_text.split() if not identifier_number else []
        _digit_positions = [i for i, t in enumerate(_tokens) if re.search(r'\d', t)]
        for i in _digit_positions:
            for lo, hi in ((i - 1, i), (i, i + 1), (i - 1, i + 1), (i - 2, i), (i, i + 2)):
                lo = max(lo, 0)
                hi = min(hi, len(_tokens) - 1)
                if lo > hi:
                    continue
                window = " ".join(_tokens[lo:hi + 1])
                if window and window != query and window not in modelno_query_variants:
                    modelno_query_variants.append(window)

        model_no_hits = []
        seen_hit_ids = set()
        for q_variant in modelno_query_variants:
            hits = client.collections["modelNos"].documents.search({
                "q": q_variant,
                "query_by": "modelNo",
                "per_page": _MODELNO_SEARCH_PER_PAGE,
                "sort_by": "_text_match:desc",
                "num_typos": 0,
                "include_fields": "materialId,modelNo"
            }).get("hits", [])
            for h in hits:
                mid = h["document"].get("materialId")
                if mid not in seen_hit_ids:
                    model_no_hits.append(h)
                    seen_hit_ids.add(mid)

        padded_query = " " + query + " "
        for hit in model_no_hits:
            mno_doc = hit["document"]
            model_no = (mno_doc.get("modelNo") or "").strip().lower()
            if not model_no:
                continue

            if not re.search(r'\d', model_no):
                continue

            model_no_parts = re.findall(r'[a-z]+|\d+', model_no)
            flexible_model_no = r'[\s\-]*'.join(re.escape(p) for p in model_no_parts) if model_no_parts else re.escape(model_no)
            pattern = r'(?<!\w)' + flexible_model_no + r'(?!\w)'
            if not re.search(pattern, padded_query):
                continue

            if not identifier_number and _THREAD_LIKE_MODEL_NO.match(model_no):
                thread_guess = attribute_matching.extract_metric_thread_attributes(q2).get("diameter", {})
                if (thread_guess.get("display") or "").strip().lower() == model_no:
                    continue

            if not identifier_number and re.fullmatch(r'\d{1,2}', model_no):
                continue

            mid = mno_doc.get("materialId")
            if mid is None:
                continue

            found_doc = None
            is_temp = False
            try:
                found_doc = client.collections["spec"].documents[str(mid)].retrieve()
            except Exception:
                try:
                    found_doc = client.collections["temp"].documents[str(mid)].retrieve()
                    is_temp = True
                except Exception:
                    continue

            if not found_doc:
                continue

            known_model_results.append({
                'productName': found_doc.get('productName', ''),
                'brandName': found_doc.get('brandName', ''),
                'variantName': found_doc.get('variantName', ''),
                'categoryName': found_doc.get('categoryName', ''),
                'MaterialId': found_doc.get('materialId', ''),
                'productSpecification': found_doc.get('productSpecification', ''),
                'listPrice': found_doc.get('listPrice', ''),
                'shortDescription': found_doc.get('shortDescription', ''),
                'UOM': found_doc.get('UOM', ''),
                'vendors': [] if is_temp else found_doc.get('vendors', []),
                'ARCvendors': found_doc.get('ARCvendors', []),
                'isTemporary': found_doc.get('isTemporary', 'true' if is_temp else 'false')
            })
    except Exception:
        pass

    attribute_results = []
    query_attributes = attribute_matching.extract_query_attributes(q2)
    if query_attributes:
        attr_query_variants = expand_query(clean_query)
        attribute_filters = []
        for attr_key, normalized in query_attributes.items():
            display = normalized.get("display")
            if not display:
                continue
            display = str(display).replace('"', '')
            attribute_filters.append(f'attr_{attr_key}:={display}')

        if attribute_filters:

            def _materialAttributes_search(filter_by):
                try:
                    res = client.collections["materialAttributes"].documents.search({
                        "q": "*",
                        "query_by": "materialIdStr",
                        "filter_by": filter_by,
                        "per_page": 250
                    })
                except Exception as e:
                    print(f"[attribute matching] materialAttributes search failed: {e}")
                    return []
                return [
                    str(hit["document"]["materialId"])
                    for hit in res.get("hits", [])
                    if hit["document"].get("materialId") is not None
                ]

            all_matched_material_ids = _materialAttributes_search(" && ".join(attribute_filters))

            if not all_matched_material_ids and len(attribute_filters) > 1:
                all_matched_material_ids = _materialAttributes_search(" || ".join(attribute_filters))

            matched_material_ids = all_matched_material_ids[:100]

            if matched_material_ids:
                id_filter = "materialId:=[" + ",".join(matched_material_ids) + "]"

                attr_best = {}
                for q_variant in attr_query_variants:
                    for collection_name, is_temp in (("spec", False), ("temp", True)):
                        try:
                            text_ranked = client.collections[collection_name].documents.search({
                                "q": q_variant.strip() or "*",
                                "query_by": "productName, variantName, productSpecification, productSpecification_normalized",
                                "query_by_weights": "4,3,2,1",
                                "filter_by": id_filter,
                                "per_page": 20,
                                "prioritize_num_matching_fields": True,
                                "sort_by": "_text_match:desc",
                                "include_fields":
                                    "materialId, productName, brandName, variantName, categoryName,"
                                    "productSpecification, listPrice, UOM, shortDescription, vendors, ARCvendors"
                            })
                        except Exception as e:
                            print(f"[attribute matching] step-2 search failed for variant {q_variant!r} on {collection_name}: {e}")
                            continue

                        for hit in text_ranked.get("hits", []):
                            doc = hit["document"]
                            mid = doc.get("materialId")
                            if mid is None:
                                continue

                            variant_words = [w for w in q_variant.lower().split() if len(w) > 1 and w.isalpha()]
                            haystack = (str(doc.get('productName', '')) + ' ' + str(doc.get('productSpecification', ''))).lower()
                            if variant_words and not all(w in haystack for w in variant_words):
                                continue

                            score = hit.get("text_match", 0)
                            if mid not in attr_best or score > attr_best[mid][0]:
                                attr_best[mid] = (score, doc, is_temp)

                for mid, (score, doc, is_temp) in sorted(attr_best.items(), key=lambda kv: kv[1][0], reverse=True):
                    attribute_results.append({
                        'productName': doc.get('productName', ''),
                        'brandName': doc.get('brandName', ''),
                        'variantName': doc.get('variantName', ''),
                        'categoryName': doc.get('categoryName', ''),
                        'MaterialId': mid,
                        'productSpecification': doc.get('productSpecification', ''),
                        'listPrice': doc.get('listPrice', ''),
                        'shortDescription': doc.get('shortDescription', ''),
                        'UOM': doc.get('UOM', ''),
                        'vendors': [] if is_temp else doc.get('vendors', []),
                        'ARCvendors': doc.get('ARCvendors', []),
                        'isTemporary': 'true' if is_temp else 'false'
                    })

    output = []
    seen = set()
    if material_id:
        try:
            doc = client.collections['spec'].documents[material_id].retrieve()
            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'shortDescription': doc.get('shortDescription', ''),
                'UOM': doc.get('UOM', ''),
                'vendors': doc.get('vendors', []),
                'ARCvendors': doc.get('ARCvendors', []),
                'isTemporary': doc.get('isTemporary', 'false')
            })
            seen.add(int(material_id))
        except typesense.exceptions.ObjectNotFound:
            pass

    if temp_material_id:
        try:
            doc = client.collections['temp'].documents[temp_material_id].retrieve()
            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'shortDescription': doc.get('shortDescription', ''),
                'UOM': doc.get('UOM', ''),
                'vendors': [],
                'ARCvendors': doc.get('ARCvendors', []),
                'isTemporary': doc.get('isTemporary', 'true')
            })
            seen.add(int(temp_material_id))
        except typesense.exceptions.ObjectNotFound:
            pass

    try:
        erp_results = client.collections["spec"].documents.search({
            "q": query,
            "query_by": "companyERPCodes",
            "filter_by": f"companyERPCodes:={query}",
            "per_page": 20
        })

        for hit in erp_results["hits"]:
            doc = hit["document"]
            mid = doc["materialId"]
            if mid in seen:
                continue
            seen.add(mid)
            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'shortDescription': doc.get('shortDescription', ''),
                'UOM': doc.get('UOM', ''),
                'vendors': doc.get('vendors', []),
                'ARCvendors': doc.get('ARCvendors', []),
                'isTemporary': 'false'
            })
    except Exception:
        pass

    try:
        temp_erp_results = client.collections["temp"].documents.search({
            "q": query,
            "query_by": "companyERPCodes",
            "filter_by": f"companyERPCodes:={query}",
            "per_page": 20
        })

        for hit in temp_erp_results["hits"]:
            doc = hit["document"]
            mid = doc["materialId"]
            if mid in seen:
                continue
            seen.add(mid)
            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'shortDescription': doc.get('shortDescription', ''),
                'UOM': doc.get('UOM', ''),
                'vendors': [],
                'ARCvendors': doc.get('ARCvendors', []),
                'isTemporary': 'true'
            })
    except Exception:
        pass

    output.sort(key=erp_sort_priority)
    if output:
        return jsonify(attach_vmi_tags(output, client))


    def search_model_number(candidate, extra_text=""):
        found = []
        found_seen = set()

        extra_text = (extra_text or "").strip()

        model_queries = []
        if extra_text:
            model_queries.append(f"{extra_text} model no {candidate}")
            model_queries.append(f"{extra_text} {candidate}")

        model_queries += [
            f"model no {candidate}",
            f"part no {candidate}",
            candidate
        ]

        for q in model_queries:
            results = client.collections['spec'].documents.search({
                'q': q,
                'query_by': 'productSpecification',
                'per_page': 20,
                'sort_by': '_text_match:desc',
                'num_typos': 0,
                'drop_tokens_threshold': 0,
                'include_fields':
                    'materialId, productName, brandName, variantName, categoryName,'
                    'productSpecification, listPrice, UOM, shortDescription, vendors, vendors.companyName,'
                    'vendors.contractPrice, vendors.vrcListPrice,'
                    'vendors.leadTime, vendors.VRC, ARCvendors, ARCvendors.UnitPrice,'
                    'ARCvendors.branchName, ARCvendors.arcLeadTime, ARCvendors.arcLeadTime,'
                    'ARCvendors.validityPeriod, ARCvendors.companyName'
            })

            for hit in results['hits']:

                doc = hit['document']
                if doc["materialId"] in found_seen:
                    continue

                found_seen.add(doc["materialId"])

                found.append({
                    'productName': doc.get('productName', ''),
                    'brandName': doc.get('brandName', ''),
                    'variantName': doc.get('variantName', ''),
                    'categoryName': doc.get('categoryName', ''),
                    'MaterialId': doc.get('materialId', ''),
                    'productSpecification': doc.get('productSpecification', ''),
                    'listPrice': doc.get('listPrice', ''),
                    'shortDescription': doc.get('shortDescription', ''),
                    'UOM': doc.get('UOM', ''),
                    'vendors': doc.get('vendors', []),
                    'ARCvendors': doc.get('ARCvendors', []),
                    'isTemporary': 'false'
                })
        return found

    if identifier_number:
        output_model = search_model_number(identifier_number, extra_text=clean_query)
        if output_model:
            return jsonify(attach_vmi_tags(output_model, client))

    model_candidate_results = []
    if model_candidate:
        model_candidate_results = search_model_number(model_candidate)

    expanded_queries = expand_query(clean_query)

    for dv in dimension_variants:
        dv_clean = parse_query(dv)['query'].strip()
        if dv_clean and dv_clean not in expanded_queries:
            expanded_queries.append(dv_clean)

    output = []
    seen = set()


    for q in expanded_queries:
        print(q)
        parsed2 = parse_query(q)
        q_brand = parsed2["brand"] or brand
        attributes = parsed2["attributes"]
        search_q = q.strip() or "*"
        search_parameters = {
            "q": search_q,
            "query_by": "productName, variantName, productSpecification, productSpecification_normalized",
            "query_by_weights": "4,3,2,1",
            "per_page": 100,
            "prioritize_num_matching_fields": True,
            "sort_by": "_text_match:desc",
            "include_fields": "materialId, productName, brandName, variantName, categoryName,"
                            "productSpecification, listPrice, UOM, shortDescription,"
                            "vendors, ARCvendors"
        }

        filters = []

        if q_brand:
            filters.append(f"brandName:={q_brand}")

        if filters:
            search_parameters["filter_by"] = " && ".join(filters)

        results = client.collections["spec"].documents.search(search_parameters)

        for hit in results["hits"]:

            doc = hit["document"]
            material_id = doc["materialId"]

            if material_id in seen:
                continue

            seen.add(material_id)

            output.append({
                "productName": doc.get("productName", ""),
                "brandName": doc.get("brandName", ""),
                "variantName": doc.get("variantName", ""),
                "categoryName": doc.get("categoryName", ""),
                "MaterialId": material_id,
                "productSpecification": doc.get("productSpecification", ""),
                "listPrice": doc.get("listPrice", ""),
                "shortDescription": doc.get("shortDescription", ""),
                "UOM": doc.get("UOM", ""),
                "vendors": doc.get("vendors", []),
                "ARCvendors": doc.get("ARCvendors", []),
            })

    if len(output) < 30:

        remaining = 30 - len(output)

        for q in expanded_queries:

            temp_params = {
                "q": q,
                "query_by": "productName, variantName, productSpecification, productSpecification_normalized",
                "query_by_weights": "4,3,2,1",
                "per_page": remaining,
                "prioritize_num_matching_fields": True,
                "sort_by": "_text_match:desc",
                "include_fields": "materialId, productName, brandName, variantName,"
                                "categoryName, productSpecification, listPrice,"
                                "UOM, shortDescription, ARCvendors, isTemporary"
            }

            temp_results = client.collections["temp"].documents.search(temp_params)

            for hit in temp_results["hits"]:

                doc = hit["document"]
                material_id = doc["materialId"]

                if material_id in seen:
                    continue

                seen.add(material_id)

                output.append({
                    "productName": doc.get("productName", ""),
                    "brandName": doc.get("brandName", ""),
                    "variantName": doc.get("variantName", ""),
                    "categoryName": doc.get("categoryName", ""),
                    "MaterialId": material_id,
                    "productSpecification": doc.get("productSpecification", ""),
                    "listPrice": doc.get("listPrice", ""),
                    "shortDescription": doc.get("shortDescription", ""),
                    "UOM": doc.get("UOM", ""),
                    "vendors": [],
                    "ARCvendors": doc.get("ARCvendors", []),
                    "isTemporary": doc.get("isTemporary", "true")
                })

                if len(output) == 30:
                    break

            if len(output) == 30:
                break

    output.sort(key=sort_priority)

    if query_attributes:
        dim_check_ids = list({
            str(o['MaterialId']) for o in (model_candidate_results + output)
            if o.get('MaterialId') is not None
        })[:100]
        conflicting_ids = set()
        if dim_check_ids:
            try:
                dim_filter = "materialId:=[" + ",".join(dim_check_ids) + "]"
                dim_check_results = client.collections["materialAttributes"].documents.search({
                    "q": "*",
                    "query_by": "materialIdStr",
                    "filter_by": dim_filter,
                    "per_page": 100
                })
                for hit in dim_check_results.get("hits", []):
                    doc = hit["document"]
                    mid = doc.get("materialId")
                    if mid is None:
                        continue
                    for attr_key, normalized in query_attributes.items():
                        display = normalized.get("display")
                        if not display:
                            continue
                        catalog_val = doc.get(f"attr_{attr_key}")
                        if catalog_val is not None and str(catalog_val) != str(display):
                            conflicting_ids.add(str(mid))
                            break
            except Exception as e:
                print(f"[dimension deprioritization] lookup failed: {e}")

        if conflicting_ids:
            model_candidate_results = soft_demote(model_candidate_results, conflicting_ids)
            output = soft_demote(output, conflicting_ids)

    known_model_results.sort(key=sort_priority)
    attribute_results.sort(key=sort_priority)
    model_candidate_results.sort(key=sort_priority)

    if known_model_results or attribute_results or model_candidate_results:
        combined = list(known_model_results)
        combined_ids = {o['MaterialId'] for o in combined}
        for o in attribute_results:
            if o['MaterialId'] not in combined_ids:
                combined.append(o)
                combined_ids.add(o['MaterialId'])
        for o in model_candidate_results:
            if o['MaterialId'] not in combined_ids:
                combined.append(o)
                combined_ids.add(o['MaterialId'])
        for o in output:
            if o['MaterialId'] not in combined_ids:
                combined.append(o)
                combined_ids.add(o['MaterialId'])
        output = combined

    output.sort(key=lambda o: 1 if is_temporary(o) else 0)

    output = output[:30]
    return jsonify(attach_vmi_tags(output, client))

if __name__ == "__main__":
    app.run(debug=True, threaded=True)