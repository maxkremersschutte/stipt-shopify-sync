import csv
import json
import os
import urllib.request
from html import unescape

SOURCE = 'https://www.stiptpolishpointshop.nl/products.json?limit=250'
OUTPUT = 'stipt_matrixify.csv'


def fetch_products():
    request = urllib.request.Request(SOURCE, headers={'User-Agent': 'Mozilla/5.0 (compatible; CatalogSync/1.0)'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    products = data.get('products', [])
    if not products:
        raise RuntimeError('Geen producten gevonden; CSV wordt niet overschreven.')
    if len(products) >= 250:
        raise RuntimeError('Mogelijk meer dan 250 producten: paginering eerst controleren.')
    return products


def main():
    products = fetch_products()
    columns = ['Handle', 'Title', 'Body HTML', 'Vendor', 'Variant SKU', 'Variant Price', 'Image Src', 'Image Position', 'Source Available', 'Price Review Needed']
    rows = []
    for product in products:
        variants = product.get('variants') or []
        images = product.get('images') or []
        for index, variant in enumerate(variants):
            price = variant.get('price', '')
            compare = variant.get('compare_at_price')
            review = bool(compare and float(compare) > float(price))
            row = {
                'Handle': product.get('handle', ''),
                'Title': product.get('title', ''),
                'Body HTML': product.get('body_html', ''),
                'Vendor': product.get('vendor', 'Stipt'),
                'Variant SKU': variant.get('sku') or '',
                'Variant Price': compare if review else price,
                'Image Src': images[0].get('src', '') if index == 0 and images else '',
                'Image Position': 1 if index == 0 and images else '',
                'Source Available': str(bool(variant.get('available'))).lower(),
                'Price Review Needed': str(review).lower(),
            }
            rows.append(row)
        for position, img in enumerate(images[1:], start=2):
            rows.append({'Handle': product.get('handle', ''), 'Image Src': img.get('src', ''), 'Image Position': position})
    with open(OUTPUT, 'w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f'{len(products)} producten verwerkt, {len(rows)} CSV-regels opgeslagen in {OUTPUT}.')
    print('LET OP: Dit is een controlebestand, niet direct importeren in Shopify.')


if __name__ == '__main__':
    main()
