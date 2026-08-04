from flask import Flask, render_template, request, jsonify
import typesense
import pandas as pd
import re
import os
from dotenv import load_dotenv
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
    "-" : " ",
    "screw driver": "screwdriver",
    "zz": "2 z",
    "core of cable":"number of cores",
    "cu":"copper",
    "hexagonal ": "hex ",
    "v belt": "v-belt",
    "belt v": "v-belt",
}

def expand_query(q):
    q = q.lower()
    variants = [q]

    # pair
    # v1 = re.sub(r'(\d+)\s+pair\b', r'\1pair', q)
    # v2 = re.sub(r'(\d+)pair\b', r'\1 pair', q)

    # # traid
    # v3 = re.sub(r'(\d+)\s+traid\b', r'\1traid', q)
    # v4 = re.sub(r'(\d+)traid\b', r'\1 traid', q)

    # a-56 -> a56
    v5 = re.sub(r'\b([a-ln-z]+)-(\d+)\b', r'\1\2', q)

    # # a-56 -> a 56
    v6 = re.sub(r'\b([a-ln-z]+)-(\d+)\b', r'\1 \2', q)

    # a 56 -> a56
    # v7 = re.sub(r'\b([a-ln-z]+)\s+(\d+)\b', r'\1\2', q)

    # # # a 56 -> a-56
    # v8 = re.sub(r'\b([a-ln-z]+)\s+(\d+)\b', r'\1-\2', q)

    # # a56 -> a 56
    v9 = re.sub(r'\b([a-ln-z]+)(\d+)\b', r'\1 \2', q)

    # # a56 -> a-56
    v10 = re.sub(r'\b([a-ln-z]+)(\d+)\b', r'\1-\2', q)

    # m 14 -> 14
    v11 = re.sub(r'\bm\s+(\d+)\b', r'\1', q)

    for v in [v5, v6, v9, v10, v11]: #, v6, v8, v9, v10
        if v not in variants:
            variants.append(v)

    # synonyms
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

    return variants

SEPARATE_NUMBER_WORDS = {
    "core", "sqmm", "sq", "mm", "mm2", "inch", "pin", "amp", "kv", "v", "w", "ltr", "kg", "gm", "m"}

WORDS_PATTERN = "|".join(map(re.escape, SEPARATE_NUMBER_WORDS))

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

    # 0 = permanent, 1 = temporary  -> permanent block always comes first
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

    # vendor tier leads; permanent wins the tie within a tier
    return (vendor_rank, 1 if is_temporary(product) else 0)

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
    model_number = None

    if query_lower.isdigit():
        num = int(query_lower)
        if 100000 <= num <= 800000:
            material_id = query_lower
        if len(query_lower) == 7:
            temp_material_id = query_lower

    match = re.search(
        r'\b(?:model\s*no\.?|model\s*number|model#)\s*([a-z0-9\-\/]+)',
        query_lower,
        flags=re.IGNORECASE
    )

    if match:
        model_number = match.group(1).lower()
        query_lower = query_lower.replace(match.group(0), " ")

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
        "temp_material_id": temp_material_id,   # new
        "model_number": model_number,
        "brand": brand,
        "attributes": attributes,
        "query": " ".join(remaining)
    }

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
    # print(query)
    query = query.replace(",", " ")
    query = query.replace("mm2", "sqmm")
    query = query.replace("(", " ")
    query = query.replace(")", " ")
    query = query.replace(":", " ")
    query = re.sub(r'\bbrands?\b', ' ', query)
    query = " ".join(query.split())
    parsed = parse_query(query)
    query = separate_number_words(query)
# print(query)
    k = 0
    # a"xb"
    m_frac = re.search(r'(\d+(?:-\d+)?/\d+)"?\s*[xX\*]\s*(\d+(?:-\d+)?(?:/\d+)?)"?', query)
    if m_frac:
        k=1
        d1 = m_frac.group(1)
        d2 = m_frac.group(2)
        query = re.sub(r'(\d+(?:-\d+)?(?:/\d+)?)"?\s*[xX\*]\s*(\d+(?:-\d+)?(?:/\d+)?)"?',
            f'{d1} inch diameter {d2} inch length', query)

    # axb, a x b, aXb, a*b and similar
    m = re.search(r'\b(\d+)\s*[xX\*]\s*(\d+)\b', query)
    if m:
        k=1
        d1 = m.group(1)
        d2 = m.group(2)
        query = re.sub(r'\b\d+\s*[xX\*]\s*\d+\b', f'{d1} length {d2} diameter', query)

    parsed1 = parse_query(query)
    material_id = parsed["material_id"]
    temp_material_id = parsed.get("temp_material_id")
    model_number = parsed['model_number']
    brand = parsed['brand']
    attributes = parsed['attributes']
    clean_query = parsed1['query']
    # ----------------------------
    # Material Id Priority Search
    # ----------------------------
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

    # ----------------------------
    # Temporary Material Id Priority Search
    # ----------------------------
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
    # ----------------------------
    # ERP Code Priority Search
    # ----------------------------
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

    # new: same ERP lookup against temp collection
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
    
    # ----------------------------
    # Model Number Priority Search
    # ----------------------------
    if model_number:

        results = client.collections['spec'].documents.search({
            'q': model_number,
            'query_by': 'productSpecification',
            'per_page': 20,
            'sort_by': '_text_match:desc',
            'include_fields':
                'materialId, productName, brandName, variantName, categoryName,'
                'productSpecification, listPrice, UOM, shortDescription, vendors, vendors.companyName,'
                'vendors.contractPrice, vendors.vrcListPrice,'
                'vendors.leadTime, vendors.VRC, ARCvendors, ARCvendors.UnitPrice,'
                'ARCvendors.branchName, ARCvendors.arcLeadTime, ARCvendors.arcLeadTime,'
                'ARCvendors.validityPeriod, ARCvendors.companyName'
        })

        output = []

        for hit in results['hits']:

            doc = hit['document']

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
                'ARCvendors': doc.get('ARCvendors', [])
            })

        output.sort(key=sort_priority)

        if output:
            return jsonify(attach_vmi_tags(output, client))

    expanded_queries = expand_query(clean_query)
    # if k==0:
    #     expanded_queries = expand_query(clean_query)
    # else:
    #     expanded_queries = list(dict.fromkeys([q2] + expand_query(clean_query)))

    # expanded_queries.append(q2)

    output = []
    seen = set()
    # ----------------------------
    # Search SPEC collection only
    # ----------------------------
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
            "per_page": 30,
            "prioritize_num_matching_fields": True,
            "sort_by": "_text_match:desc",
            "include_fields": "materialId, productName, brandName, variantName, categoryName,"
                            "productSpecification, listPrice, UOM, shortDescription,"
                            "vendors, ARCvendors"
        }

        filters = []

        if q_brand:
            filters.append(f"brandName:={brand}")

        for attr in attributes:
            if attr == "core":
                filters.append("(productSpecification:core || productSpecification:cores)")
            else:
                filters.append(f"productSpecification:{attr}")

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
                "ARCvendors": doc.get("ARCvendors", [])
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
    output = output[:30]
    return jsonify(attach_vmi_tags(output, client))

if __name__ == "__main__":
    app.run(debug=True)