import os
import sys
import time
import logging
from decimal import Decimal, InvalidOperation

import requests

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
SHOP = os.getenv('SHOPIFY_SHOP_DOMAIN', '').replace('https://', '').replace('http://', '').strip().strip('/')
if SHOP != 'jsmtools.myshopify.com':
    sys.exit('Unexpected or missing SHOPIFY_SHOP_DOMAIN')
for key in ('SHOPIFY_CLIENT_ID', 'SHOPIFY_CLIENT_SECRET'):
    if not os.getenv(key):
        sys.exit(f'Missing GitHub secret: {key}')

API = f'https://{SHOP}/admin/api/2026-10/graphql.json'
SOURCE = 'https://www.stiptpolishpointshop.nl/products.json?limit=250'
SESSION = requests.Session()


def request(method, url, **kwargs):
    for attempt in range(5):
        try:
            response = SESSION.request(method, url, timeout=45, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt * 2, 25))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt * 2)
    raise RuntimeError('Request failed after retries')


token_response = request('POST', f'https://{SHOP}/admin/oauth/access_token', data={
    'grant_type': 'client_credentials',
    'client_id': os.environ['SHOPIFY_CLIENT_ID'],
    'client_secret': os.environ['SHOPIFY_CLIENT_SECRET'],
})
TOKEN = token_response.get('access_token')
if not TOKEN:
    sys.exit('Shopify did not return an access token')

# Shopify may not list implied read scopes separately when write scopes are granted.
scopes = {x.strip() for x in token_response.get('scope', '').split(',') if x.strip()}
required = {'write_products', 'write_inventory', 'read_locations'}
if scopes and not required.issubset(scopes):
    sys.exit('Missing Shopify scopes: ' + ', '.join(sorted(required - scopes)))


def gql(query, variables=None):
    result = request('POST', API, headers={'X-Shopify-Access-Token': TOKEN}, json={
        'query': query,
        'variables': variables or {},
    })
    if result.get('errors'):
        raise RuntimeError(str(result['errors']))
    return result['data']


source = request('GET', SOURCE).get('products', [])
if len(source) < 100:
    sys.exit(f'Supplier returned only {len(source)} products; sync aborted')
source_ids = [str(p['id']) for p in source]
if len(source_ids) != len(set(source_ids)):
    sys.exit('Duplicate supplier product IDs; sync aborted')
logging.info('Supplier products: %s', len(source))

locations = gql('query { locations(first: 50) { nodes { id name isActive } } }')['locations']['nodes']
active = [x for x in locations if x['isActive']]
location_id = os.getenv('SHOPIFY_LOCATION_ID', '').strip()
if not location_id:
    if len(active) != 1:
        sys.exit('Multiple active Shopify locations: set SHOPIFY_LOCATION_ID as a GitHub secret')
    location_id = active[0]['id']
if location_id not in {x['id'] for x in active}:
    sys.exit('SHOPIFY_LOCATION_ID is not an active Shopify location')

# Only products with our persistent supplier ID are safe to update.
# We deliberately do not match by SKU or title, which may belong to another supplier.
existing = {}
cursor = None
while True:
    page = gql('''query($after:String) {
      products(first:100, after:$after, query:"tag:Stipt automatische sync") {
        pageInfo { hasNextPage endCursor }
        nodes {
          id title status
          metafield(namespace:"custom", key:"stipt_source_id") { value }
        }
      }
    }''', {'after': cursor})['products']
    for product in page['nodes']:
        source_id = (product.get('metafield') or {}).get('value')
        if source_id:
            if source_id in existing:
                sys.exit(f'Duplicate existing source ID {source_id}; sync aborted')
            existing[source_id] = product
    if not page['pageInfo']['hasNextPage']:
        break
    cursor = page['pageInfo']['endCursor']

MUTATION = '''mutation($input:ProductSetInput!) {
  productSet(input:$input, synchronous:true) {
    product { id title status }
    userErrors { field message }
  }
}'''

created = updated = skipped = failed = 0
for product in source:
    title = product.get('title') or '(untitled)'
    source_id = str(product['id'])
    old = existing.get(source_id)
    variants = product.get('variants') or []
    if not variants or len(variants) > 100:
        logging.warning('SKIP %s: invalid variant count', title)
        skipped += 1
        continue
    try:
        for variant in variants:
            price = Decimal(str(variant['price']))
            if not price.is_finite() or price < 0:
                raise ValueError('invalid price')
            compare = variant.get('compare_at_price')
            if compare not in (None, '') and Decimal(str(compare)) > price:
                raise ValueError('discounted product; regular price not verified')
    except (ValueError, TypeError, KeyError, InvalidOperation) as error:
        logging.warning('SKIP %s: %s', title, error)
        skipped += 1
        continue

    options = product.get('options') or []
    # productSet requires optionValues for every variant, including Default Title.
    if not options and len(variants) == 1:
        options = [{'name': 'Title', 'position': 1, 'values': ['Default Title']}]
    definitions = []
    for option in options:
        values = [{'name': str(v)} for v in option.get('values', []) if v]
        if values:
            definitions.append({'name': option['name'], 'position': option['position'], 'values': values})

    variant_inputs = []
    for variant in variants:
        item = {
            'price': str(variant['price']),
            'inventoryPolicy': 'DENY',
            'inventoryItem': {'tracked': True},
            'inventoryQuantities': [{
                'locationId': location_id,
                'name': 'available',
                'quantity': 100 if variant.get('available') else 0,
            }],
        }
        if variant.get('sku'):
            item['sku'] = str(variant['sku'])
        item['optionValues'] = [
            {'optionName': option['name'],
             'name': str(variant.get('option' + str(option['position'])) or ('Default Title' if option['name'] == 'Title' and len(variants) == 1 else ''))}
            for option in options
        ]
        if not item['optionValues'] or any(not value['name'] for value in item['optionValues']):
            raise ValueError(f'Missing option value for {title}')
        variant_inputs.append(item)

    payload = {
        'title': title,
        'descriptionHtml': product.get('body_html') or '',
        'vendor': 'Stipt',
        'status': old['status'] if old else 'DRAFT',
        'tags': ['Stipt', 'Stipt automatische sync'],
        'metafields': [{
            'namespace': 'custom', 'key': 'stipt_source_id',
            'type': 'single_line_text_field', 'value': source_id,
        }],
        'variants': variant_inputs,
    }
    if old:
        payload['id'] = old['id']
    if definitions:
        payload['productOptions'] = definitions
    if not old:
        image_urls = [image.get('src') for image in product.get('images', []) if image.get('src')]
        if image_urls:
            payload['files'] = [
                {'originalSource': url, 'contentType': 'IMAGE', 'alt': title}
                for url in image_urls[:15]
            ]
    try:
        result = gql(MUTATION, {'input': payload})['productSet']
        if result.get('userErrors'):
            raise RuntimeError(str(result['userErrors']))
        if not result.get('product'):
            raise RuntimeError('No product returned')
        if old:
            updated += 1
            logging.info('UPDATED %s', title)
        else:
            created += 1
            logging.info('CREATED DRAFT %s', title)
    except Exception as error:
        failed += 1
        logging.error('FAILED %s: %s', title, error)
    time.sleep(0.4)

logging.info('Finished: created drafts=%s updated=%s skipped=%s failed=%s', created, updated, skipped, failed)
if failed:
    sys.exit(1)

