"""BuggyShop - a deliberately flawed demo shop used ONLY to test Simulant locally.

Planted bugs (ground truth for measuring detection):
 UX     U1 icon-only cart link (no label)        U2 low-contrast grey text / tiny print
        U3 'Proceed to PG' jargon + pay button with no loading state (3.5s)
        U4 vague form error 'Error E422' at the top, tiny    U5 cookie banner covers bottom buttons (esp. on mobile)
 LOGIC  L1 coupon SAVE20 can be applied repeatedly (stacking)   L2 negative / zero quantity accepted
        L3 server trusts the client-sent total (hidden field)   L4 /success shows 'order confirmed' without paying
        L5 double-click on pay creates duplicate orders
"""
import asyncio
import uuid

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()
PRODUCTS = {
    1: ("Phone Case", 499, "Shock-absorbing case with a matte finish."),
    2: ("Wireless Earbuds", 1499, "Bluetooth 5.3, 20 hours of battery."),
    3: ("Steel Water Bottle", 699, "Keeps drinks cold for 24 hours."),
}
CARTS: dict[str, dict] = {}
ORDERS: list[dict] = []

CSS = """
body{font-family:Arial,sans-serif;margin:0;color:#222}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;background:#1f3a5f;color:#fff}
header a{color:#fff;text-decoration:none;margin-left:14px}
main{padding:20px;max-width:900px;margin:auto}
.grid{display:flex;gap:16px;flex-wrap:wrap}.card{border:1px solid #ddd;padding:12px;width:230px;border-radius:6px}
.btn{background:#e8590c;color:#fff;border:0;padding:10px 16px;border-radius:4px;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block}
.muted{color:#c4c4c4;font-size:13px}.fine{color:#b5b5b5;font-size:10px}
.link-grey{color:#cfcfcf;font-size:13px}
input{padding:8px;margin:4px 0;font-size:14px}
#cookie{position:fixed;left:0;right:0;bottom:0;height:90px;background:#333;color:#fff;padding:10px 20px;z-index:50;font-size:13px}
#cookie button{font-size:10px;color:#aaa;background:#333;border:1px solid #555;padding:2px 6px}
.err{color:#c00;font-size:11px}
table{border-collapse:collapse;width:100%}td,th{padding:6px;border-bottom:1px solid #eee;text-align:left}
"""


def sid_of(request: Request) -> str:
    return request.cookies.get("sid") or uuid.uuid4().hex


def cart_of(sid: str) -> dict:
    return CARTS.setdefault(sid, {"items": {}, "coupons": 0})


def totals(cart: dict):
    sub = sum(PRODUCTS[p][1] * q for p, q in cart["items"].items())
    disc = round(sub * 0.20 * cart["coupons"], 2)               # L1: every SAVE20 stacks another 20%
    return sub, disc, round(sub - disc, 2)


def page(body: str, sid: str, title="BuggyShop", count: int = 0) -> HTMLResponse:
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head><body>
<header><a href="/"><b>BuggyShop</b></a><span><a href="/">Home</a>
<a href="/cart"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l3 12h11l2-8H6"/></svg></a></span></header>
<main>{body}</main>
<div id="cookie">We use cookies to improve your experience. By continuing you accept our policy.
<button onclick="document.getElementById('cookie').remove()">ok</button></div></body></html>"""
    r = HTMLResponse(html)
    r.set_cookie("sid", sid, httponly=True)
    return r


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    sid = sid_of(request)
    cards = "".join(
        f'<div class="card"><h3>{n}</h3><p class="fine">{d}</p><p>₹{p}</p><a class="btn" href="/product/{i}">View</a></div>'
        for i, (n, p, d) in PRODUCTS.items())
    return page(f'<h1>Welcome</h1><p class="muted">Free delivery on orders above ₹999 · SKU-coded catalogue</p><div class="grid">{cards}</div>', sid)


@app.get("/product/{pid}", response_class=HTMLResponse)
def product(pid: int, request: Request):
    sid = sid_of(request)
    n, p, d = PRODUCTS[pid]
    return page(f"""<h1>{n}</h1><p>{d}</p><p><b>₹{p}</b></p>
<form method="post" action="/cart/add"><input type="hidden" name="pid" value="{pid}">
<input name="qty" type="number" value="1" aria-label="Quantity"> <button class="btn" type="submit">Add to cart</button></form>
<p class="fine">Prices inclusive of GST. Stock subject to availability.</p>""", sid, n)


@app.post("/cart/add")
def cart_add(request: Request, pid: int = Form(...), qty: int = Form(1)):
    sid = sid_of(request)
    c = cart_of(sid)
    c["items"][pid] = c["items"].get(pid, 0) + qty                # L2: no validation of qty
    r = RedirectResponse("/cart", status_code=303)
    r.set_cookie("sid", sid, httponly=True)
    return r


@app.get("/cart", response_class=HTMLResponse)
def cart(request: Request, msg: str = ""):
    sid = sid_of(request)
    c = cart_of(sid)
    sub, disc, total = totals(c)
    rows = "".join(
        f'<tr><td>{PRODUCTS[p][0]}</td><td>₹{PRODUCTS[p][1]}</td><td><form method="post" action="/cart/qty" style="display:flex;gap:6px">'
        f'<input type="hidden" name="pid" value="{p}"><input name="qty" type="number" value="{q}" aria-label="Quantity for {PRODUCTS[p][0]}"><button class="btn" type="submit">Update</button></form></td></tr>'
        for p, q in c["items"].items()) or '<tr><td colspan="3">Your cart is empty.</td></tr>'
    note = f'<p style="color:green">{msg}</p>' if msg else ""
    return page(f"""<h1>Your cart</h1>{note}<table><tr><th>Item</th><th>Price</th><th>Quantity</th></tr>{rows}</table>
<p>Subtotal: ₹{sub}</p><p>Discount: ₹{disc}</p><p><b>Total: ₹{total}</b></p>
<form method="post" action="/cart/coupon"><input name="code" placeholder="Coupon code"> <button class="btn" type="submit">Apply</button></form>
<p class="muted">Hint: SAVE20 gives 20% off.</p><p><a class="link-grey" href="/checkout">Continue to checkout</a></p>""", sid, "Cart")


@app.post("/cart/qty")
def cart_qty(request: Request, pid: int = Form(...), qty: int = Form(...)):
    sid = sid_of(request)
    cart_of(sid)["items"][pid] = qty                              # L2: negative / zero accepted
    return RedirectResponse("/cart", status_code=303)


@app.post("/cart/coupon")
def cart_coupon(request: Request, code: str = Form(...)):
    sid = sid_of(request)
    if code.strip().upper() == "SAVE20":
        cart_of(sid)["coupons"] += 1                              # L1: never checks if already used
        return RedirectResponse("/cart?msg=Coupon+SAVE20+applied", status_code=303)
    return RedirectResponse("/cart?msg=Invalid+coupon", status_code=303)


@app.get("/checkout", response_class=HTMLResponse)
def checkout(request: Request):
    sid = sid_of(request)
    _, _, total = totals(cart_of(sid))
    return page(f"""<h1>Checkout</h1><div id="top" class="err"></div>
<p>Amount payable: ₹{total}</p>
<input type="hidden" id="total" name="total" value="{total}">
<input id="name" placeholder="Full name"><br><input id="address" placeholder="Delivery address"><br><input id="phone" placeholder="Phone"><br>
<button class="btn" id="pay" onclick="pay()">Proceed to PG</button>
<script>
function pay(){{
  fetch('/api/pay',{{method:'POST',headers:{{'Content-Type':'application/json'}},credentials:'same-origin',
    body:JSON.stringify({{name:document.getElementById('name').value,address:document.getElementById('address').value,phone:document.getElementById('phone').value,total:document.getElementById('total').value}})}})
  .then(r=>r.json()).then(d=>{{ if(d.error){{document.getElementById('top').textContent='Error '+d.error;}} else {{location='/success?order='+d.order_id;}} }});
}}
</script>""", sid, "Checkout")


@app.post("/api/pay")
async def pay(request: Request):
    sid = sid_of(request)
    body = await request.json()
    await asyncio.sleep(3.5)                                      # U3: slow, and the page shows no loading state
    if not str(body.get("phone", "")).isdigit() or len(str(body.get("phone"))) != 10:
        return JSONResponse({"error": "E422"})                    # U4: vague error
    order = {"id": len(ORDERS) + 1, "sid": sid, "total": body.get("total"), "items": dict(cart_of(sid)["items"])}   # L3: trusts client total
    ORDERS.append(order)                                          # L5: no idempotency
    return {"order_id": order["id"]}


@app.get("/success", response_class=HTMLResponse)
def success(request: Request, order: str = ""):                   # L4: no check that an order exists
    sid = sid_of(request)
    amt = next((o["total"] for o in ORDERS if str(o["id"]) == order), "")
    return page(f"<h1>Thank you! Your order is confirmed.</h1><p>Order #{order or 'N/A'}. Amount charged: ₹{amt}</p><a class='btn' href='/'>Continue shopping</a>", sid, "Order confirmed")


@app.get("/api/_debug/orders")
def debug_orders():
    return ORDERS
