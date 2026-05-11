"""
Sports Card eBay Sniper - Browse API Edition
Uses eBay Browse API (no rate limit issues) + auto token refresh.
Run: python3 app.py  -->  opens http://127.0.0.1:5000
"""

from flask import Flask, render_template_string, jsonify, request, Response, stream_with_context
from datetime import datetime

app = Flask(__name__)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

# ── Config ────────────────────────────────────────────────────────────────────
config = {
    "app_id":              "",
    "cert_id":             "",
    "categories":          ["213"],   # 213=Baseball, 214=Hockey
    "min_discount_pct":    15.0,
    "min_discount_dollar": 5.0,
    "grade_filters":       [],
    "type_filters":        [],
    "max_price":           0,
    "min_comps":           2,
    "scan_interval":       120,
}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                config.update(json.load(f))
            print(f"[Config] Loaded (App ID: {'set' if config['app_id'] else 'not set'})")
        except Exception as e:
            print(f"[Config] Load error: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"[Config] Save error: {e}")

load_config()

# ── Runtime state ─────────────────────────────────────────────────────────────
deals        = []
seen_ids     = set()
deal_queue   = queue.Queue()
comp_cache   = {}
CACHE_TTL    = 7200  # 2 hours

scanner_running = False
scan_status = {
    "status": "stopped", "last_scan": None,
    "total_checked": 0, "total_deals": 0,
    "next_scan_in": 0, "last_error": None,
}

# ── OAuth token management ────────────────────────────────────────────────────
oauth_token    = None
token_expires  = 0

def get_token():
    """Get or refresh the eBay OAuth App Access Token."""
    global oauth_token, token_expires
    if oauth_token and time.time() < token_expires - 60:
        return oauth_token
    try:
        creds = base64.b64encode(f"{config['app_id']}:{config['cert_id']}".encode()).decode()
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
            timeout=15
        )
        data = r.json()
        if "access_token" in data:
            oauth_token   = data["access_token"]
            token_expires = time.time() + data.get("expires_in", 7200)
            print("[Token] Refreshed successfully")
            return oauth_token
        else:
            print(f"[Token] Error: {data}")
            scan_status["last_error"] = str(data.get("error_description", "Token error"))
            return None
    except Exception as e:
        print(f"[Token] Exception: {e}")
        scan_status["last_error"] = str(e)
        return None

def browse_headers():
    token = get_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Content-Type": "application/json"
    }

# ── eBay Browse API calls ─────────────────────────────────────────────────────
BROWSE_API = "https://api.ebay.com/buy/browse/v1/item_summary/search"

def search_new_listings():
    """Get newest BIN listings from Browse API."""
    hdrs = browse_headers()
    if not hdrs:
        return []

    cat_ids = ",".join(config["categories"])
    filters = "buyingOptions:{FIXED_PRICE}"
    if config["max_price"] > 0:
        filters += f",price:[..{config['max_price']}]"

    params = {
        "category_ids": cat_ids,
        "filter":       filters,
        "sort":         "newlyListed",
        "limit":        100,
        "fieldgroups":  "MATCHING_ITEMS,EXTENDED",
    }
    try:
        r = requests.get(BROWSE_API, headers=hdrs, params=params, timeout=15)
        data = r.json()
        if "itemSummaries" in data:
            return data["itemSummaries"]
        elif "errors" in data:
            msg = data["errors"][0].get("message", "Browse API error")
            print(f"[Browse] Error: {msg}")
            scan_status["last_error"] = msg
        return []
    except Exception as e:
        print(f"[Browse] Exception: {e}")
        scan_status["last_error"] = str(e)
        return []


def get_price_comps(title, cat_id):
    """
    Estimate market value by searching Browse API for similar items
    and averaging their current prices. Uses aggressive caching.
    """
    stop = {"card","cards","baseball","hockey","football","basketball",
            "sports","trading","with","the","and","for","lot","bundle"}
    words = re.findall(r"[A-Za-z0-9#']+", title)
    kw    = " ".join(w for w in words if w.lower() not in stop)[:60]
    key   = re.sub(r'\s+', ' ', kw.lower()[:40])

    now = time.time()
    if key in comp_cache and now - comp_cache[key][0] < CACHE_TTL:
        return comp_cache[key][1]

    hdrs = browse_headers()
    if not hdrs:
        return []

    params = {
        "q":            kw,
        "category_ids": cat_id,
        "filter":       "buyingOptions:{FIXED_PRICE}",
        "sort":         "bestMatch",
        "limit":        20,
    }
    prices = []
    try:
        r = requests.get(BROWSE_API, headers=hdrs, params=params, timeout=10)
        data = r.json()
        for item in data.get("itemSummaries", []):
            try:
                p = float(item["price"]["value"])
                if p > 0:
                    prices.append(p)
            except:
                pass
    except Exception as e:
        print(f"[Comps] Exception: {e}")

    comp_cache[key] = (now, prices)
    return prices


def calc_market_value(prices):
    if len(prices) < 2:
        return None
    s = sorted(prices)
    if len(s) >= 5:
        trim = max(1, len(s) // 5)
        s = s[trim:-trim]
    return sum(s) / len(s)


# ── Grade / type matching ─────────────────────────────────────────────────────
GRADE_KW = {
    "PSA 10":       ["psa 10", "psa10", "psa gem", "gem 10"],
    "PSA 9":        ["psa 9 ", "psa9 ", " psa 9"],
    "PSA 8":        ["psa 8 ", "psa8 ", " psa 8"],
    "PSA 7":        ["psa 7 "],
    "PSA 6":        ["psa 6 "],
    "PSA 5":        ["psa 5 "],
    "PSA Auth":     ["psa auth"],
    "BGS 10":       ["bgs 10", "bgs10", "black label"],
    "BGS 9.5":      ["bgs 9.5", "bgs9.5"],
    "BGS 9":        ["bgs 9 ", "bgs9 "],
    "BGS 8.5":      ["bgs 8.5"],
    "BGS 8":        ["bgs 8 ", "bgs8 "],
    "SGC 10":       ["sgc 10", "sgc10"],
    "SGC 9.5":      ["sgc 9.5"],
    "SGC 9":        ["sgc 9 "],
    "SGC 8":        ["sgc 8 "],
    "Raw/Ungraded": [],
}
GRADE_SERVICES = ["psa", "bgs", "sgc", "csg", "hga", "gai", "ags"]

TYPE_KW = {
    "Rookie":             ["rookie", " rc ", "/rc", "1st year"],
    "Auto/Signed":        ["auto", "autograph", "signed", "ink"],
    "Refractor/Prizm":    ["refractor", "prizm", "prism", "chrome"],
    "1/1 & Superfractor": ["1/1", "superfractor", "super fractor"],
    "Patch/Relic":        ["patch", "relic", "jersey", "game used", "mem "],
    "Vintage (pre-1980)": [],
    "Lot/Bulk":           ["lot", "bulk", "collection", "bundle"],
    "Short Print":        ["short print", " sp ", "/sp", "ssp"],
    "Parallel":           ["parallel", "gold", "blue", "orange", "purple", "pink", "green", "rainbow"],
    "Serial Numbered":    ["/25", "/50", "/75", "/99", "/100", "/150", "/199", "/249", "/299", "/499"],
}

def matches_grade(title):
    gf = config["grade_filters"]
    if not gf: return True
    tl = title.lower() + " "
    for g in gf:
        if g == "Raw/Ungraded":
            if not any(s in tl for s in GRADE_SERVICES): return True
        elif any(kw in tl for kw in GRADE_KW.get(g, [g.lower()])): return True
    return False

def matches_type(title):
    tf = config["type_filters"]
    if not tf: return True
    tl = title.lower() + " "
    for t in tf:
        if t == "Vintage (pre-1980)":
            if re.search(r'\b19[0-7]\d\b', title): return True
        elif any(kw in tl for kw in TYPE_KW.get(t, [t.lower()])): return True
    return False

def extract_tags(title):
    tags, tl = [], title.lower() + " "
    for g, kws in GRADE_KW.items():
        if g == "Raw/Ungraded": continue
        if any(kw in tl for kw in kws):
            tags.append({"label": g, "cls": "grade"}); break
    for t, kws in TYPE_KW.items():
        if t == "Vintage (pre-1980)":
            if re.search(r'\b19[0-7]\d\b', title):
                tags.append({"label": "Vintage", "cls": "type"})
        elif any(kw in tl for kw in kws):
            tags.append({"label": t.split("/")[0].strip(), "cls": "type"})
    return tags[:3]


# ── Scanner loop ──────────────────────────────────────────────────────────────
def scanner_loop():
    global scanner_running
    print("[Scanner] Started (Browse API mode)")

    while scanner_running:
        try:
            scan_status["status"]     = "scanning"
            scan_status["last_scan"]  = datetime.now().strftime("%I:%M:%S %p")
            scan_status["last_error"] = None

            if not config["app_id"] or not config["cert_id"]:
                print("[Scanner] Missing App ID or Cert ID")
                time.sleep(10)
                continue

            items = search_new_listings()
            print(f"[Scanner] {len(items)} listings retrieved")

            for item in items:
                if not scanner_running: break

                item_id = item.get("itemId", "")
                if not item_id or item_id in seen_ids: continue
                seen_ids.add(item_id)

                title = item.get("title", "")
                if not title: continue
                if not matches_grade(title) or not matches_type(title): continue

                try:
                    price = float(item["price"]["value"])
                except:
                    continue
                if price <= 0: continue

                cat_id = config["categories"][0]
                scan_status["total_checked"] += 1

                comps = get_price_comps(title, cat_id)
                if len(comps) < config["min_comps"]: continue

                mkt = calc_market_value(comps)
                if not mkt or mkt <= 0: continue

                disc_pct = (mkt - price) / mkt * 100
                disc_usd = mkt - price

                if disc_pct < config["min_discount_pct"]:    continue
                if disc_usd < config["min_discount_dollar"]: continue

                url        = item.get("itemWebUrl", "")
                img        = item.get("thumbnailImages", [{}])[0].get("imageUrl", "") or item.get("image", {}).get("imageUrl", "")
                best_offer = "BEST_OFFER" in item.get("buyingOptions", [])
                cat_name   = item.get("categories", [{}])[0].get("categoryName", "Card")

                deal = {
                    "id":           item_id,
                    "title":        title,
                    "price":        price,
                    "market_value": round(mkt, 2),
                    "disc_pct":     round(disc_pct, 1),
                    "disc_usd":     round(disc_usd, 2),
                    "comp_count":   len(comps),
                    "url":          url,
                    "img":          img,
                    "best_offer":   best_offer,
                    "category":     cat_name,
                    "tags":         extract_tags(title),
                    "found_at":     datetime.now().strftime("%I:%M:%S %p"),
                    "timestamp":    time.time(),
                    "listing_type": "FixedPrice",
                }

                deals.insert(0, deal)
                if len(deals) > 500: deals.pop()
                deal_queue.put(deal)
                scan_status["total_deals"] += 1
                print(f"[DEAL ✓] {title[:55]:<55} ${price:.2f} vs ${mkt:.2f} ({disc_pct:.0f}% off)")
                time.sleep(0.5)

            scan_status["status"] = "waiting"
            interval = config["scan_interval"]
            for i in range(interval):
                if not scanner_running: break
                scan_status["next_scan_in"] = interval - i
                time.sleep(1)

        except Exception as e:
            print(f"[Scanner] Error: {e}")
            scan_status["status"]     = "error"
            scan_status["last_error"] = str(e)
            time.sleep(30)

    scan_status["status"] = "stopped"
    print("[Scanner] Stopped")


# ── Flask routes ──────────────────────────────────────────────────────────────
HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")).read()

@app.route("/")
def index(): return HTML

@app.route("/api/config", methods=["GET","POST"])
def api_config():
    if request.method == "POST":
        config.update(request.json or {})
        save_config()
        return jsonify({"ok": True})
    return jsonify(config)

@app.route("/api/start", methods=["POST"])
def api_start():
    global scanner_running
    if scanner_running:
        return jsonify({"ok": True, "already": True})
    scan_status["total_checked"] = 0
    scan_status["total_deals"]   = 0
    scanner_running = True
    threading.Thread(target=scanner_loop, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global scanner_running
    scanner_running = False
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify({**scan_status, "running": scanner_running, "deal_count": len(deals)})

@app.route("/api/deals")
def api_deals():
    since = float(request.args.get("since", 0))
    return jsonify([d for d in deals if d["timestamp"] > since])

@app.route("/api/clear", methods=["POST"])
def api_clear():
    deals.clear()
    return jsonify({"ok": True})

@app.route("/stream")
def stream():
    def generate():
        yield f"data: {json.dumps({'type':'connected'})}\n\n"
        while True:
            try:
                deal = deal_queue.get(timeout=20)
                yield f"data: {json.dumps({'type':'deal','deal':deal})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})

if __name__ == "__main__":
    print("\n" + "="*52)
    print("  🃏  Sports Card eBay Sniper  (Browse API)")
    print("  Opening http://127.0.0.1:5000 ...")
    print("="*52 + "\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
