import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

BASE = "https://www.trinasty.sk"
API = "/wp-json/wc/store/v1/products"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 MagranoFeedBuilder/1.1"
PARENT_FIELDS = "id,name,slug,type,permalink,sku,prices,images,categories,brands,attributes,variations,is_in_stock,is_on_backorder,low_stock_remaining,stock_availability,is_purchasable,on_sale,short_description,description"
VARIATION_FIELDS = "id,name,parent,variation,permalink,sku,prices,images,is_in_stock,is_on_backorder,low_stock_remaining,stock_availability,is_purchasable,on_sale,description"
VAT = "23%"
DESCRIPTION_LIMIT = 3000
ALT_IMAGES = 3
TEXT_IN_STOCK = "Skladom"
TEXT_BACKORDER = "Na objednávku"
TEXT_OUT_OF_STOCK = "Nie je skladom"
LABEL_SALE = "Akcia"


def log(msg):
    sys.stderr.write(datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
    sys.stderr.flush()


def http_get(url, delay, attempts=4):
    last = None
    for i in range(attempts):
        if i:
            wait = 5 * (3 ** (i - 1))
            log(f"retry {i} after {wait}s: {url} ({last})")
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "sk,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, list):
                    raise RuntimeError(f"unexpected API answer: {str(payload)[:200]}")
                time.sleep(delay)
                return payload, headers
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 120))
            if e.code in (400, 401, 403, 404) and i >= 1:
                break
        except Exception as e:
            last = repr(e)
    raise RuntimeError(f"request failed: {url} ({last})")


def fetch_pages(base, query, fields, delay, data_dir, prefix, reuse):
    pages = []
    page = 1
    total_pages = None
    total_items = None
    while True:
        cached = os.path.join(data_dir, f"{prefix}_{page}.json") if data_dir else None
        if reuse and cached and os.path.exists(cached):
            with open(cached, encoding="utf-8") as fh:
                payload = json.load(fh)
            items, meta = payload["items"], payload["meta"]
        else:
            q = dict(query)
            q.update({"per_page": 100, "page": page, "orderby": "id", "order": "asc", "_fields": fields})
            url = base + API + "?" + urllib.parse.urlencode(q)
            items, headers = http_get(url, delay)
            meta = {"total": headers.get("x-wp-total"), "pages": headers.get("x-wp-totalpages")}
            if cached:
                with open(cached, "w", encoding="utf-8") as fh:
                    json.dump({"items": items, "meta": meta}, fh, ensure_ascii=False)
        if total_pages is None:
            total_pages = int(meta.get("pages") or 0)
            total_items = int(meta.get("total") or 0)
            if total_pages < 1 or total_items < 1:
                raise RuntimeError(f"{prefix}: missing X-WP-Total / X-WP-TotalPages headers, cannot paginate safely")
            log(f"{prefix}: {total_items} items on {total_pages} pages")
        pages.extend(items)
        log(f"{prefix}: page {page}/{total_pages} ({len(items)} items)")
        if not items or page >= total_pages:
            break
        page += 1
    return pages, total_items


def fetch_categories(base, delay, data_dir, reuse):
    cached = os.path.join(data_dir, "categories.json") if data_dir else None
    if reuse and cached and os.path.exists(cached):
        with open(cached, encoding="utf-8") as fh:
            return json.load(fh)
    cats = []
    page = 1
    while True:
        url = base + API + "/categories?" + urllib.parse.urlencode({"per_page": 100, "page": page})
        items, headers = http_get(url, delay)
        cats.extend(items)
        pages = int(headers.get("x-wp-totalpages") or 0)
        if not items or (pages and page >= pages) or (not pages and len(items) < 100):
            break
        page += 1
    if cached:
        with open(cached, "w", encoding="utf-8") as fh:
            json.dump(cats, fh, ensure_ascii=False)
    return cats


TAG_BLOCK = re.compile(r"<(script|style|iframe|noscript|svg)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S)
TAG_BREAK = re.compile(r"</?(p|br|li|ul|ol|div|h[1-6]|tr|table|section|article|blockquote)\b[^>]*>", re.I)
TAG_ANY = re.compile(r"<[^>]+>")
INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏  ⁠﻿]")
SPACES = re.compile(r"[ \t\r\f\v   ]+")
NEWLINES = re.compile(r"\s*\n\s*")
SKU_TEXTURIZE = [(" – ", " - "), ("–", "--"), ("—", "---"), ("″", '"'), ("′", "'"), ("×", "x"), ("…", "..."),
                 ("“", '"'), ("”", '"'), ("„", '"'), ("‘", "'"), ("’", "'"), ("‚", "'")]


def unescape_twice(value):
    first = html.unescape(value)
    return html.unescape(first) if "&" in first else first


def clean_text(value, limit=DESCRIPTION_LIMIT):
    if not value:
        return ""
    text = TAG_BLOCK.sub(" ", value)
    text = TAG_BREAK.sub("\n", text)
    text = TAG_ANY.sub(" ", text)
    text = unescape_twice(text)
    text = INVISIBLE.sub("", text)
    text = SPACES.sub(" ", text)
    text = NEWLINES.sub("\n", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        text = (cut[:space] if space > limit * 0.6 else cut).rstrip() + "…"
    return text


def clean_name(value):
    return SPACES.sub(" ", INVISIBLE.sub("", unescape_twice(value or ""))).strip()


def clean_sku(value):
    sku = clean_name(value)
    for typographic, plain in SKU_TEXTURIZE:
        sku = sku.replace(typographic, plain)
    return sku


def ascii_url(value):
    return urllib.parse.quote(value or "", safe=":/?=&%+,;@#~")


def clean_url(value):
    if not value or "?" not in value:
        return ascii_url(value)
    base, query = value.split("?", 1)
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    fixed = [(k, urllib.parse.unquote(v)) for k, v in pairs]
    return ascii_url(base) + "?" + urllib.parse.urlencode(fixed, encoding="utf-8")


def money(prices, key):
    raw = (prices or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        minor = int((prices or {}).get("currency_minor_unit", 2))
        value = int(raw) / (10 ** minor)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def fmt_price(value):
    return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value)}"


def category_path(cat_id, cats_by_id, memo):
    if cat_id in memo:
        return memo[cat_id]
    chain = []
    seen = set()
    cur = cat_id
    while cur and cur in cats_by_id and cur not in seen:
        seen.add(cur)
        chain.append(clean_name(cats_by_id[cur]["name"]))
        cur = cats_by_id[cur].get("parent") or 0
    chain.reverse()
    memo[cat_id] = chain
    return chain


def product_categories(product, cats_by_id, memo):
    result = []
    for c in product.get("categories") or []:
        path = category_path(c.get("id"), cats_by_id, memo) or [clean_name(c.get("name"))]
        result.append((c.get("id"), " | ".join(path)))
    result.sort(key=lambda x: -x[1].count(" | "))
    return result


def brand_of(product):
    brands = product.get("brands") or []
    if brands:
        return clean_name(brands[0].get("name")), ascii_url(brands[0].get("link") or "")
    for a in product.get("attributes") or []:
        if (a.get("taxonomy") or "").endswith("znacka-brand") or clean_name(a.get("name")).lower() in ("značka", "znacka", "brand", "výrobca"):
            terms = a.get("terms") or []
            if terms:
                return clean_name(terms[0].get("name")), ""
    return "", ""


def simple_params(product):
    params = []
    for a in product.get("attributes") or []:
        name = clean_name(a.get("name")).rstrip(":").strip()
        if (a.get("taxonomy") or "").endswith("znacka-brand") or name.lower() in ("značka", "znacka", "brand"):
            continue
        for t in a.get("terms") or []:
            val = clean_name(t.get("name"))
            if name and val:
                params.append((name, val))
    return params


def variation_params(text):
    params = []
    for part in re.split(r",\s*(?=[^:,]+:+\s)", clean_name(text)):
        m = re.match(r"\s*([^:]+?)\s*:+\s*(.+?)\s*$", part)
        if m and m.group(1) and m.group(2):
            params.append((m.group(1), m.group(2)))
    return params


def stock_of(obj):
    text = clean_name(((obj.get("stock_availability") or {}).get("text")) or "")
    in_stock = bool(obj.get("is_in_stock"))
    backorder = bool(obj.get("is_on_backorder"))
    if not text:
        text = TEXT_IN_STOCK if in_stock else (TEXT_BACKORDER if backorder else TEXT_OUT_OF_STOCK)
    return in_stock, backorder, text, obj.get("low_stock_remaining")


def item_from(obj, parent, name, group_title, desc, images, categories, brand, brand_url, sku, params):
    current = money(obj.get("prices"), "price")
    regular = money(obj.get("prices"), "regular_price")
    if current is None:
        return None
    on_sale = bool(obj.get("on_sale")) and regular is not None and current < regular
    in_stock, backorder, stock_text, low_stock = stock_of(obj)
    return {
        "id": obj["id"],
        "group_id": parent["id"] if parent else None,
        "title": name,
        "group_title": group_title,
        "description": desc,
        "url": clean_url(obj.get("permalink")) or clean_url((parent or {}).get("permalink")),
        "images": images,
        "img_small": images[0][1] if images else "",
        "price": regular if on_sale else current,
        "sale_price": current if on_sale else None,
        "on_sale": on_sale,
        "brand": brand,
        "brand_url": brand_url,
        "categories": categories,
        "sku": sku,
        "in_stock": in_stock,
        "backorder": backorder,
        "stock_text": stock_text,
        "low_stock": low_stock,
        "params": params,
    }


def image_list(obj):
    out = []
    for i in obj.get("images") or []:
        if i.get("src"):
            out.append((ascii_url(i.get("src")), ascii_url(i.get("thumbnail") or i.get("src"))))
    return out


def build_items(parents, variations, cats):
    cats_by_id = {c["id"]: c for c in cats}
    memo = {}
    vars_by_parent = {}
    for v in variations:
        vars_by_parent.setdefault(v.get("parent"), []).append(v)
    stats = {"simple": 0, "variants": 0, "variable_without_variants": 0, "skipped_no_price": 0,
             "skipped_not_purchasable": 0, "skipped_no_image": 0, "skipped_no_category": 0, "orphan_variations": 0,
             "no_sku": 0, "on_sale": 0, "backorder": 0}
    parent_ids = {p["id"] for p in parents}
    stats["orphan_variations"] = sum(1 for v in variations if v.get("parent") not in parent_ids)
    items = []
    brands = {}
    for p in parents:
        if not p.get("is_purchasable", True):
            stats["skipped_not_purchasable"] += 1
            continue
        name = clean_name(p.get("name"))
        desc = clean_text(p.get("description")) or clean_text(p.get("short_description"))
        images = image_list(p)
        categories = product_categories(p, cats_by_id, memo)
        brand, brand_url = brand_of(p)
        if brand:
            brands[brand] = brands.get(brand) or brand_url
        sku = clean_sku(p.get("sku"))
        if not images:
            stats["skipped_no_image"] += 1
            log(f"skipped {p['id']} {name}: no image")
            continue
        if not categories:
            stats["skipped_no_category"] += 1
            log(f"skipped {p['id']} {name}: no category")
            continue
        if not sku:
            stats["no_sku"] += 1
        children = vars_by_parent.get(p["id"]) if p.get("type") == "variable" else None
        if children:
            for v in children:
                if not v.get("is_purchasable", True):
                    stats["skipped_not_purchasable"] += 1
                    continue
                vparams = variation_params(v.get("variation"))
                vname = name + (" – " + ", ".join(val for _, val in vparams) if vparams else "")
                vimages = image_list(v) or images
                vdesc = clean_text(v.get("description")) or desc
                vsku = clean_sku(v.get("sku")) or sku
                it = item_from(v, p, vname, name, vdesc, vimages, categories, brand, brand_url, vsku, vparams)
                if it is None:
                    stats["skipped_no_price"] += 1
                    continue
                items.append(it)
                stats["variants"] += 1
                stats["on_sale"] += int(it["on_sale"])
                stats["backorder"] += int(it["backorder"])
            continue
        if p.get("type") == "variable":
            stats["variable_without_variants"] += 1
        it = item_from(p, None, name, "", desc, images, categories, brand, brand_url, sku, simple_params(p))
        if it is None:
            stats["skipped_no_price"] += 1
            continue
        items.append(it)
        stats["simple"] += 1
        stats["on_sale"] += int(it["on_sale"])
        stats["backorder"] += int(it["backorder"])
    return items, brands, stats


def available_now(it):
    return it["in_stock"] and not it["backorder"]


def el(tag, value):
    return f"<{tag}>{escape(str(value))}</{tag}>" if value not in (None, "") else ""


def render_magrano(it):
    parts = ["<PRODUCT>", el("ITEM_ID", it["id"]), el("TITLE", it["title"]), "<CATEGORIES>"]
    for cat_id, path in it["categories"]:
        attr = f" id={quoteattr(str(cat_id))}" if cat_id else ""
        parts.append(f"<CATEGORY{attr}>{escape(path)}</CATEGORY>")
    parts.append("</CATEGORIES>")
    parts.append(el("URL", it["url"]))
    parts.append(el("IMG", it["img_small"]))
    parts.append(el("PRICE", fmt_price(it["price"])))
    parts.append(el("BRAND", it["brand"]))
    if it["sale_price"] is not None:
        parts.append(el("SALE_PRICE", fmt_price(it["sale_price"])))
    if available_now(it):
        parts.append(el("DELIVERY_DATE", 0))
    parts.append(el("DELIVERY_TEXT", it["stock_text"]))
    if it["on_sale"]:
        parts.append("<LABELS><LABEL>" + el("LABEL_NAME", LABEL_SALE) + "</LABEL></LABELS>")
    if it["group_id"]:
        parts.append(el("ITEM_GROUP_ID", it["group_id"]))
        parts.append(el("GROUP_TITLE", it["group_title"]))
    parts.append(el("DESCRIPTION", it["description"]))
    params = list(it["params"])
    if it["sku"]:
        params.insert(0, ("SKU", it["sku"]))
    if params:
        parts.append("<PARAMS>" + "".join("<PARAM>" + el("PARAM_KEY", k) + el("PARAM_VALUE", v) + "</PARAM>" for k, v in params) + "</PARAMS>")
    parts.append("</PRODUCT>")
    return "".join(p for p in parts if p)


def render_heureka(it):
    parts = ["<SHOPITEM>", el("ITEM_ID", it["id"])]
    if it["group_id"]:
        parts.append(el("ITEMGROUP_ID", it["group_id"]))
    parts += [el("PRODUCTNAME", it["title"]), el("PRODUCT", it["title"]), el("DESCRIPTION", it["description"]), el("URL", it["url"])]
    if it["images"]:
        parts.append(el("IMGURL", it["images"][0][0]))
        parts += [el("IMGURL_ALTERNATIVE", u) for u, _ in it["images"][1:1 + ALT_IMAGES]]
    parts += [el("PRICE_VAT", fmt_price(it["sale_price"] if it["sale_price"] is not None else it["price"])), el("VAT", VAT),
              el("MANUFACTURER", it["brand"]), el("CATEGORYTEXT", it["categories"][0][1] if it["categories"] else ""),
              el("PRODUCTNO", it["sku"]), el("DELIVERY_DATE", 0 if available_now(it) else "")]
    parts += ["<PARAM>" + el("PARAM_NAME", k) + el("VAL", v) + "</PARAM>" for k, v in it["params"]]
    parts.append("</SHOPITEM>")
    return "".join(p for p in parts if p)


def render_categories(cats):
    cats_by_id = {c["id"]: c for c in cats}
    memo = {}
    out = []
    for c in sorted(cats, key=lambda c: (len(category_path(c["id"], cats_by_id, memo)), c["id"])):
        path = category_path(c["id"], cats_by_id, memo)
        out.append("<CATEGORY>" + el("TITLE", clean_name(c.get("name"))) + el("URL", ascii_url(c.get("permalink") or c.get("link") or ""))
                   + el("ID", c["id"]) + el("HIERARCHY", " | ".join(path)) + el("PARENT_ID", c.get("parent") or 0) + "</CATEGORY>")
    return out


def render_brands(brands):
    return ["<BRAND>" + el("TITLE", name) + el("URL", url) + "</BRAND>" for name, url in sorted(brands.items()) if url]


def count_existing_items(path, item_tag):
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(f"<{item_tag}>"):
                n += 1
    return n


def write_xml(path, root, rows):
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f'<?xml version="1.0" encoding="utf-8"?>\n<{root}>\n')
            for row in rows:
                fh.write(row)
                fh.write("\n")
            fh.write(f"</{root}>\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def sibling(path, suffix):
    stem, ext = os.path.splitext(path)
    return stem + suffix + (ext or ".xml")


def main():
    ap = argparse.ArgumentParser(description="Product, category and brand XML feeds of trinasty.sk built from the public WooCommerce Store API")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "trinasty-produkty.xml"))
    ap.add_argument("--format", choices=["magrano", "heureka"], default="magrano")
    ap.add_argument("--out-categories", default="")
    ap.add_argument("--out-brands", default="")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--data", default="")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--min-ratio", type=float, default=0.9)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    args.base = args.base.rstrip("/")
    if args.data:
        os.makedirs(args.data, exist_ok=True)
    started = time.time()
    parents, parents_total = fetch_pages(args.base, {}, PARENT_FIELDS, args.delay, args.data, "parents", args.reuse)
    variations, variations_total = fetch_pages(args.base, {"type": "variation"}, VARIATION_FIELDS, args.delay, args.data, "variations", args.reuse)
    cats = fetch_categories(args.base, args.delay, args.data, args.reuse)
    log(f"downloaded: {len(parents)}/{parents_total} parents, {len(variations)}/{variations_total} variations, {len(cats)} categories")
    if len(parents) < parents_total * args.min_ratio:
        raise SystemExit(f"only {len(parents)} of {parents_total} parents downloaded, feed not written")
    if len(variations) < variations_total * args.min_ratio:
        raise SystemExit(f"only {len(variations)} of {variations_total} variations downloaded, feed not written")
    if not cats:
        raise SystemExit("no categories downloaded, feed not written")
    items, brands, stats = build_items(parents, variations, cats)
    if not items:
        raise SystemExit("no items built, feed not written")
    previous = count_existing_items(args.out, "SHOPITEM" if args.format == "heureka" else "PRODUCT")
    if previous and len(items) < previous * 0.8 and not args.force:
        raise SystemExit(f"new feed has {len(items)} items, previous had {previous}; refusing to overwrite (use --force)")
    if args.format == "heureka":
        write_xml(args.out, "SHOP", (render_heureka(it) for it in items))
    else:
        write_xml(args.out, "PRODUCTS", (render_magrano(it) for it in items))
        cat_path = args.out_categories or sibling(args.out, "-kategorie")
        brand_path = args.out_brands or sibling(args.out, "-znacky")
        if cat_path.lower() != "none":
            write_xml(cat_path, "CATEGORIES", render_categories(cats))
            stats["categories_file"] = cat_path
        if brand_path.lower() != "none":
            write_xml(brand_path, "BRANDS", render_brands(brands))
            stats["brands_file"] = brand_path
    stats["items"] = len(items)
    stats["parents"] = len(parents)
    stats["variations"] = len(variations)
    stats["categories"] = len(cats)
    stats["brands"] = len(brands)
    stats["format"] = args.format
    stats["seconds"] = round(time.time() - started)
    stats["bytes"] = os.path.getsize(args.out)
    stats["generated"] = datetime.now().isoformat(timespec="seconds")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
