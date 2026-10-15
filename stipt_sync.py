import os, sys, time, json, logging
from decimal import Decimal
import requests

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
SHOP = os.getenv('SHOPIFY_SHOP_DOMAIN','jsmtools.myshopify.com').replace('https://','').strip('/')
API = f'https://{SHOP}/admin/api/2026-10/graphql.json'
SOURCE = 'https://www.stiptpolishpointshop.nl/products.json?limit=250'
S = requests.Session()

def request(method, url, **kw):
    for n in range(5):
        try:
            r=S.request(method,url,timeout=45,**kw)
            if r.status_code in (429,500,502,503,504):
                time.sleep(min(2**n*2,25));continue
            r.raise_for_status();return r.json()
        except (requests.Timeout,requests.ConnectionError):
            if n==4:raise
            time.sleep(2**n*2)
    raise RuntimeError('Request failed: '+url)

def gql(q, variables=None):
    data=request('POST',API,headers={'X-Shopify-Access-Token':TOKEN},json={'query':q,'variables':variables or {}})
    if data.get('errors'):raise RuntimeError(str(data['errors']))
    return data['data']

for key in ('SHOPIFY_CLIENT_ID','SHOPIFY_CLIENT_SECRET'):
    if not os.getenv(key):sys.exit(f'Missing secret: {key}')
if SHOP != 'jsmtools.myshopify.com':sys.exit('Unexpected shop domain')

token_data=request('POST',f'https://{SHOP}/admin/oauth/access_token',data={'grant_type':'client_credentials','client_id':os.environ['SHOPIFY_CLIENT_ID'],'client_secret':os.environ['SHOPIFY_CLIENT_SECRET']})
TOKEN=token_data.get('access_token')
if not TOKEN:sys.exit('Shopify authentication failed (check app/store organization and scopes)')
required={'read_products','write_products','read_inventory','write_inventory','read_locations'}
scopes=set(token_data.get('scope','').split(','))
if not required.issubset(scopes):sys.exit('Missing Shopify scopes: '+', '.join(sorted(required-scopes)))

source=request('GET',SOURCE).get('products',[])
if len(source)<100:sys.exit(f'Source returned only {len(source)} products; refusing to sync')
ids=[p['id'] for p in source]
if len(set(ids))!=len(ids):sys.exit('Duplicate supplier IDs')

locs=gql('query { locations(first: 50) { nodes { id name isActive } } }')['locations']['nodes']
loc_id=os.getenv('SHOPIFY_LOCATION_ID','')
active=[x for x in locs if x['isActive']]
if not loc_id:
    if len(active)!=1:sys.exit('Multiple active locations: set SHOPIFY_LOCATION_ID GitHub secret to the correct location GID')
    loc_id=active[0]['id']
if loc_id not in {x['id'] for x in active}:sys.exit('Location ID not found or inactive')

existing={}
cursor=None
while True:
    data=gql('query($after:String){ products(first:100,after:$after,query:"vendor:Stipt") { pageInfo { hasNextPage endCursor } nodes { id handle title vendor metafield(namespace:"custom",key:"stipt_source_id") { value } variants(first:100) { nodes { id sku } } } } }',{'after':cursor})['products']
    for p in data['nodes']:
        sid=(p.get('metafield') or {}).get('value')
        if sid:existing[str(sid)]=p
    if not data['pageInfo']['hasNextPage']:break
    cursor=data['pageInfo']['endCursor']

# Do not guess regular prices: any discounted variant is skipped until verified.
# For non-discounted variants the public price is used as displayed by supplier.
MUT='''mutation($input:ProductSetInput!){ productSet(input:$input,synchronous:true){ product { id title } userErrors { field message } } }'''
created=updated=skipped=failed=0
for p in source:
    sid=str(p['id']); old=existing.get(sid)
    variants=p.get('variants') or []
    if not variants or len(variants)>100:
        logging.warning('Skip %s: missing/too many variants',p.get('title'));skipped+=1;continue
    if any(v.get('compare_at_price') and Decimal(str(v['compare_at_price']))>Decimal(str(v['price'])) for v in variants):
        logging.warning('Skip discounted product %s: regular price requires verification',p['title']);skipped+=1;continue
    if any(v.get('price') is None for v in variants):
        logging.warning('Skip missing price %s',p['title']);skipped+=1;continue
    opts=p.get('options') or []
    if len(opts)==1 and opts[0].get('name')=='Title' and all(v.get('title')=='Default Title' for v in variants):opts=[]
    option_defs=[]
    for o in opts:
        values=[{'name':str(v)} for v in o.get('values',[]) if v]
        if values:option_defs.append({'name':o['name'],'position':o['position'],'values':values})
    vs=[]
    for v in variants:
        x={'price':str(v['price']),'inventoryPolicy':'DENY','inventoryItem':{'tracked':True},'inventoryQuantities':[{'locationId':loc_id,'name':'available','quantity':100 if v.get('available') else 0}]}
        if v.get('sku'):x['sku']=str(v['sku'])
        if option_defs:x['optionValues']=[{'optionName':o['name'],'name':str(v.get('option'+str(o['position'])))} for o in opts if v.get('option'+str(o['position']))]
        vs.append(x)
    inp={'title':p['title'],'descriptionHtml':p.get('body_html') or '', 'vendor':'Stipt','status':'DRAFT' if not old else 'ACTIVE','tags':['Stipt','Stipt automatische sync'], 'metafields':[{'namespace':'custom','key':'stipt_source_id','type':'single_line_text_field','value':sid}], 'variants':vs}
    if old:inp['id']=old['id']
    if option_defs:inp['productOptions']=option_defs
    if not old:
        imgs=[i.get('src') for i in p.get('images',[]) if i.get('src')]
        if imgs:inp['files']=[{'originalSource':u,'contentType':'IMAGE','alt':p['title']} for u in imgs[:15]]
    try:
        result=gql(MUT,{'input':inp})['productSet']
        if result['userErrors']:raise RuntimeError(str(result['userErrors']))
        if not result['product']:raise RuntimeError('No product returned')
        if old:updated+=1
        else:created+=1
        logging.info('%s %s', 'Updated' if old else 'Created DRAFT',p['title'])
    except Exception as e:
        failed+=1;logging.error('Failed %s: %s',p['title'],e)
    time.sleep(0.4)
logging.info('Done: created drafts=%s updated=%s skipped=%s failed=%s',created,updated,skipped,failed)
if failed:sys.exit(1)

