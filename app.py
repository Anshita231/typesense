from flask import Flask, render_template, request, jsonify
import typesense
import pandas as pd
import re
import os
from dotenv import load_dotenv
load_dotenv()

def expand_query(q):

    q = q.lower()
    variants = [q]

    # pair
    v1 = re.sub(r'(\d+)\s+pair\b', r'\1pair', q)
    v2 = re.sub(r'(\d+)pair\b', r'\1 pair', q)

    # traid
    v3 = re.sub(r'(\d+)\s+traid\b', r'\1traid', q)
    v4 = re.sub(r'(\d+)traid\b', r'\1 traid', q)

    for v in [v1, v2, v3, v4]:
        if v not in variants:
            variants.append(v)

    return variants

def sort_priority(product):

    has_arc = len(product.get("ARCvendors", [])) > 0
    has_vrc = len(product.get("vendors", [])) > 0

    if has_arc:
        return 0

    if has_vrc:
        return 1

    return 2

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
# print(f"Loaded {len(KNOWN_BRANDS)} brands")
# print(f"Loaded {len(KNOWN_ATTRIBUTES)} attributes")

def parse_query(query):

    query_lower = query.lower()
    material_id = None
    model_number = None

    if query_lower.isdigit(): 
        num = int(query_lower) 
        if 100000 <= num <= 800000: 
            material_id = query_lower

    match = re.search(
        r'\b(?:model\s*no\.?|model\s*number|model#)\s*([a-z0-9\-\/]+)',
        query_lower,
        flags=re.IGNORECASE
    )

    if match:
        model_number = match.group(1).lower()
        query_lower = query_lower.replace(
            match.group(0),
            " "
        )

    words = query_lower.split()

    brand = None
    attributes = []
    remaining = []

    for word in words:
        if word in KNOWN_BRANDS:
            brand = word
        elif word in KNOWN_ATTRIBUTES:
            attributes.append(word)
        else:
            remaining.append(word)
    return {
        "material_id": material_id,
        "model_number": model_number,
        "brand": brand,
        "attributes": attributes,
        "query": " ".join(remaining)
    }
#PART NO
# print(os.getenv("TYPESENSE_HOST"))
# print(os.getenv("TYPESENSE_API_KEY"))
client = typesense.Client({
    'nodes': [{
        'host': "x5y9s0ilj2gvh8p7p-1.a1.typesense.net",
        'port': '443',
        'protocol': 'https'
    }],
    'api_key': "QXyEXsatg9YJc30P37deOA4qN9gyN6zF",
    'connection_timeout_seconds': 20
})

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/search")

def search():

    query = request.args.get("q", "")

    parsed = parse_query(query)

    material_id = parsed["material_id"]
    model_number = parsed['model_number']
    brand = parsed['brand']
    attributes = parsed['attributes']
    clean_query = parsed['query']

    # ----------------------------
    # Material Id Priority Search
    # ----------------------------
    if material_id:
        try:
            doc = client.collections['product'].documents[
                material_id
            ].retrieve()

            output = [{
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'vendors': doc.get('vendors', []),
                'ARCvendors': doc.get('ARCvendors', [])
            }]

            return jsonify(output)

        except typesense.exceptions.ObjectNotFound:
            pass

    # ----------------------------
    # ERP Code Priority Search
    # ----------------------------
    try:

        erp_results = client.collections["erp_mapping"].documents.search({
            "q": query,
            "query_by": "companyERPCode",
            "filter_by": f"companyERPCode:={query}",
            "per_page": 20
        })

        if erp_results["found"] > 0:

            output = []
            seen = set()

            for hit in erp_results["hits"]:

                material_id = hit["document"]["materialId"]

                if material_id in seen:
                    continue

                seen.add(material_id)

                doc = client.collections["product"].documents[
                    str(material_id)
                ].retrieve()

                output.append({
                    'productName': doc.get('productName', ''),
                    'brandName': doc.get('brandName', ''),
                    'variantName': doc.get('variantName', ''),
                    'categoryName': doc.get('categoryName', ''),
                    'MaterialId': doc.get('materialId', ''),
                    'productSpecification': doc.get('productSpecification', ''),
                    'listPrice': doc.get('listPrice', ''),
                    'vendors': doc.get('vendors', []),
                    'ARCvendors': doc.get('ARCvendors', [])
                })

            output.sort(key=sort_priority)

            if output:
                return jsonify(output)

    except Exception:
        pass
    # ----------------------------
    # Model Number Priority Search
    # ----------------------------
    if model_number:

        results = client.collections['product'].documents.search({
            'q': model_number,
            'query_by': 'productSpecification',
            'per_page': 20,
            'sort_by': '_text_match:desc',
            'include_fields':
                'materialId, productName, brandName, variantName, categoryName,'
                'productSpecification, listPrice, vendors, vendors.companyName,'
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
                'vendors': doc.get('vendors', []),
                'ARCvendors': doc.get('ARCvendors', [])
            })

        output.sort(key=sort_priority)

        if output:
            return jsonify(output)

    expanded_queries = expand_query(clean_query)

    output = []
    seen = set()

    for q in expanded_queries:

        search_parameters = {
            'q': q,
            'query_by': 'productName, variantName, productSpecification',
            'query_by_weights': '3,2,1',
            'per_page': 20,
            'prioritize_num_matching_fields': True,
            'sort_by': '_text_match:desc',
            'include_fields':
                'materialId, productName, brandName, variantName, categoryName,'
                'productSpecification, listPrice, vendors, vendors.companyName,'
                'vendors.contractPrice, vendors.discount, vendors.vrcListPrice,'
                'vendors.leadTime, vendors.VRC, ARCvendors, ARCvendors.UnitPrice,'
                'ARCvendors.branchName, ARCvendors.arcLeadTime, ARCvendors.arcLeadTime,'
                'ARCvendors.validityPeriod, ARCvendors.companyName'
        }
        filters = []
        
        if brand:
            filters.append(f'brandName:={brand}')

        for attr in attributes:
            filters.append(f'productSpecification:{attr}')

        if filters:
            search_parameters['filter_by'] = " && ".join(filters)

        results = client.collections['product'].documents.search(
            search_parameters
        )

        for hit in results['hits']:

            doc = hit['document']
            material_id = doc.get('materialId')

            # remove duplicates
            if material_id in seen:
                continue

            seen.add(material_id)

            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'variantName': doc.get('variantName', ''),
                'categoryName': doc.get('categoryName', ''),
                'MaterialId': material_id,
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', ''),
                'vendors': doc.get('vendors', []),
                'ARCvendors': doc.get('ARCvendors', [])
            })
            # import pprint
            # pprint.pp(doc)
    

    output.sort(key=sort_priority)        

    return jsonify(output)

if __name__ == "__main__":
    app.run(debug=True)