from flask import Flask, render_template, request, jsonify
import typesense
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

KNOWN_BRANDS  = {
    'generic','samsung','tvs','festo','asian','skf',
    'crompton','apl','xps','fenner','unbrako','bosch','philips',
    'taparia','panasonic','polycab','anchor','stanley','siemens',
    'havells','schneider','abb','hensel','fag','ntn','legrand',
    'omron','honeywell','jainson','hager','dowells','braco',
    'comet','teknic','diamond','miranda','smc','janatics',
    'contitech','astral','zoloto','totem','supreme',
    'bonfiglioli','sick','connectwell','wago','selec',
    'hindustan','bharat','bijlee','kei','hex','kabel'
}

KNOWN_ATTRIBUTES = {
    'green','aluminium','white','stainless','steel',
    'black','copper','heavy','duty','blue','tube',
    'wire','rubber','female','male','flat','cable',
    'plastic','current','voltage','grade','brass',
    'carbon','nylon','yellow','flexible','core',
    'sqmm','belt','pole','insulated','pipe',
    'terminal','bearing','motor','digital',
    'safety','pressure','switch','plate',
    'capacitor','pump','xlpe','alloy',
    'phase','chrome','round','seal'
}

def parse_query(query):

    words = query.lower().split()

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
        "brand": brand,
        "attributes": attributes,
        "query": " ".join(remaining)
    }
print(os.getenv("TYPESENSE_HOST"))
print(os.getenv("TYPESENSE_API_KEY"))
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

    brand = parsed['brand']
    attributes = parsed['attributes']
    clean_query = parsed['query']

    expanded_queries = expand_query(clean_query)

    output = []
    seen = set()

    for q in expanded_queries:

        search_parameters = {
            'q': q,
            'query_by': 'productSpecification',
            'per_page': 5,
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