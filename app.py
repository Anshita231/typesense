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
print(f"Loaded {len(KNOWN_BRANDS)} brands")
print(f"Loaded {len(KNOWN_ATTRIBUTES)} attributes")

def parse_query(query):

    query_lower = query.lower()

    model_number = None

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
        "model_number": model_number,
        "brand": brand,
        "attributes": attributes,
        "query": " ".join(remaining)
    }

# print(os.getenv("TYPESENSE_HOST"))
# print(os.getenv("TYPESENSE_API_KEY"))
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

    parsed = parse_query(query)

    model_number = parsed['model_number']
    brand = parsed['brand']
    attributes = parsed['attributes']
    clean_query = parsed['query']

    # ----------------------------
    # Model Number Priority Search
    # ----------------------------
    if model_number:

        results = client.collections['product'].documents.search({
            'q': model_number,
            'query_by': 'productSpecification',
            'per_page': 5,
            'sort_by': '_text_match:desc'
        })

        output = []

        for hit in results['hits']:

            doc = hit['document']

            output.append({
                'productName': doc.get('productName', ''),
                'brandName': doc.get('brandName', ''),
                'MaterialId': doc.get('materialId', ''),
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', '')
            })

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
            'per_page': 7,
            'prioritize_num_matching_fields': True,
            'sort_by': '_text_match:desc',
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
                'MaterialId': material_id,
                'productSpecification': doc.get('productSpecification', ''),
                'listPrice': doc.get('listPrice', '')
            })
    return jsonify(output)


if __name__ == "__main__":
    app.run(debug=True)