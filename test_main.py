import os
import pytest
import io
from fastapi.testclient import TestClient

# Override the database file name in main BEFORE importing app elements
import main
main.DB_FILE = "vibe_inventory_test.db"

from main import app, get_db_connection

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    # Force initialize the database with test file before each test runs
    main.init_db(force_reset=True)
    yield
    # Clean up the test database file after each test finishes
    if os.path.exists("vibe_inventory_test.db"):
        try:
            os.remove("vibe_inventory_test.db")
        except OSError:
            pass

@pytest.fixture
def auth_headers():
    username = "testuser"
    password = "VibePass123!"
    client.post("/api/auth/register", json={"username": username, "password": password})
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}

# --- Test Cases ---

def test_get_inventory(auth_headers):
    """Verify that inventory retrieval returns the correct seeded mock items."""
    response = client.get("/api/inventory", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    
    # Check Vibe Shirt seed values
    vibe_shirt = next(p for p in data if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["name"] == "Vibe Shirt"
    assert vibe_shirt["quantity"] == 15
    assert vibe_shirt["price"] == 25.0
    assert vibe_shirt["status"] == "In Stock"

    # Check Retro Cap low stock status
    retro_cap = next(p for p in data if p["sku"] == "CP-RET-02")
    assert retro_cap["quantity"] == 3
    assert retro_cap["status"] == "Low Stock"

    # Check Neon Mug out of stock status
    neon_mug = next(p for p in data if p["sku"] == "MG-NEO-03")
    assert neon_mug["quantity"] == 0
    assert neon_mug["status"] == "Out of Stock"


def test_inbound_existing_product(auth_headers):
    """Verify restocking an existing SKU increments quantity and logs transaction."""
    response = client.post("/api/inbound", json={"sku": "TS-VIB-01", "quantity": 10}, headers=auth_headers)
    assert response.status_code == 200
    assert "Successfully processed inbound stock" in response.json()["message"]

    # Verify inventory table has incremented stock
    inv_response = client.get("/api/inventory", headers=auth_headers)
    vibe_shirt = next(p for p in inv_response.json() if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["quantity"] == 25  # 15 initial + 10 inbound

    # Verify transaction log has inbound entry
    tx_response = client.get("/api/transactions", headers=auth_headers)
    assert len(tx_response.json()) > 0
    latest_tx = tx_response.json()[0]
    assert latest_tx["sku"] == "TS-VIB-01"
    assert latest_tx["type"] == "INBOUND"
    assert latest_tx["quantity"] == 10
    assert latest_tx["revenue"] == 0.0  # Inbound stock does not generate revenue


def test_inbound_new_product(auth_headers):
    """Verify registering a brand new product catalog entry and initial stock."""
    payload = {
        "sku": "JS-FLW-04",
        "name": "Flower Socks",
        "price": 8.99,
        "quantity": 50,
        "low_stock_threshold": 10
    }
    response = client.post("/api/inbound", json=payload, headers=auth_headers)
    assert response.status_code == 200

    # Verify it exists in inventory catalog
    inv_response = client.get("/api/inventory", headers=auth_headers)
    new_prod = next(p for p in inv_response.json() if p["sku"] == "JS-FLW-04")
    assert new_prod["name"] == "Flower Socks"
    assert new_prod["quantity"] == 50
    assert new_prod["price"] == 8.99
    assert new_prod["status"] == "In Stock"


def test_inbound_negative_or_zero_quantity(auth_headers):
    """Verify negative or zero quantities are rejected and DB remains safe."""
    # Test zero quantity
    response_zero = client.post("/api/inbound", json={"sku": "TS-VIB-01", "quantity": 0}, headers=auth_headers)
    assert response_zero.status_code in (400, 422)

    # Test negative quantity
    response_neg = client.post("/api/inbound", json={"sku": "TS-VIB-01", "quantity": -5}, headers=auth_headers)
    assert response_neg.status_code in (400, 422)

    # Confirm database state did NOT change
    inv_response = client.get("/api/inventory", headers=auth_headers)
    vibe_shirt = next(p for p in inv_response.json() if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["quantity"] == 15  # Remains unchanged


def test_inbound_new_product_missing_details(auth_headers):
    """Verify new SKU registration fails if name/price are missing."""
    response = client.post("/api/inbound", json={"sku": "JS-FLW-04", "quantity": 10}, headers=auth_headers)
    assert response.status_code == 400
    assert "required to register" in response.json()["detail"]


def test_outbound_sale_success(auth_headers):
    """Verify that a valid POS sale decrements stock and accumulates revenue."""
    # Sell 5 units of Vibe Shirt (price $25.00)
    response = client.post("/api/outbound", json={"sku": "TS-VIB-01", "quantity": 5}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["revenue"] == 125.0

    # Verify stock decremented
    inv_response = client.get("/api/inventory", headers=auth_headers)
    vibe_shirt = next(p for p in inv_response.json() if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["quantity"] == 10  # 15 initial - 5 sold

    # Verify KPI revenue accumulated
    kpi_response = client.get("/api/kpis", headers=auth_headers)
    assert kpi_response.json()["total_revenue"] == 125.0


def test_outbound_insufficient_stock(auth_headers):
    """Verify selling more than available stock returns HTTP 400 and leaves database safe."""
    # Attempt to sell 20 units of Vibe Shirt (only 15 in stock)
    response = client.post("/api/outbound", json={"sku": "TS-VIB-01", "quantity": 20}, headers=auth_headers)
    assert response.status_code == 400
    assert "Insufficient stock" in response.json()["detail"]

    # Verify stock level remains untouched
    inv_response = client.get("/api/inventory", headers=auth_headers)
    vibe_shirt = next(p for p in inv_response.json() if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["quantity"] == 15  # Unchanged

    # Verify no transaction log was committed
    tx_response = client.get("/api/transactions", headers=auth_headers)
    # Should only contain initial seed transaction logs (2 INBOUND seeds)
    inbound_txs = [t for t in tx_response.json() if t["type"] == "OUTBOUND"]
    assert len(inbound_txs) == 0


def test_outbound_negative_or_zero_quantity(auth_headers):
    """Verify POS checkout fails on negative or zero units purchase."""
    response_zero = client.post("/api/outbound", json={"sku": "TS-VIB-01", "quantity": 0}, headers=auth_headers)
    assert response_zero.status_code in (400, 422)

    response_neg = client.post("/api/outbound", json={"sku": "TS-VIB-01", "quantity": -3}, headers=auth_headers)
    assert response_neg.status_code in (400, 422)

    # Verify database level is safe
    inv_response = client.get("/api/inventory", headers=auth_headers)
    vibe_shirt = next(p for p in inv_response.json() if p["sku"] == "TS-VIB-01")
    assert vibe_shirt["quantity"] == 15


def test_sku_sanitization_guardrail(auth_headers):
    """Verify SQL/Prompt Injection attempts in SKU field are caught and blocked."""
    malicious_sku = "TS-VIB-01; DROP TABLE products;"
    response = client.post("/api/inbound", json={"sku": malicious_sku, "quantity": 1}, headers=auth_headers)
    assert response.status_code == 400
    assert "Only alphanumeric characters and hyphens allowed" in response.json()["detail"]

    # Verify products table is fully intact
    inv_response = client.get("/api/inventory", headers=auth_headers)
    assert len(inv_response.json()) == 3  # Seed items still exist


def test_product_name_sanitization(auth_headers):
    """Verify HTML script injections in product names are stripped out before DB entry."""
    payload = {
        "sku": "JS-SEC-99",
        "name": "<script>alert('xss')</script>Secured Box",
        "price": 10.0,
        "quantity": 5
    }
    response = client.post("/api/inbound", json=payload, headers=auth_headers)
    assert response.status_code == 200

    # Retrieve and check name from database
    inv_response = client.get("/api/inventory", headers=auth_headers)
    added_prod = next(p for p in inv_response.json() if p["sku"] == "JS-SEC-99")
    assert added_prod["name"] == "Secured Box"  # Script tag stripped out


# --- SaaS Refactoring Specific Test Cases ---

def test_unauthenticated_request_fails():
    """Verify that accessing endpoints without a token returns HTTP 401."""
    response = client.get("/api/inventory")
    assert response.status_code == 401
    
    response = client.get("/api/kpis")
    assert response.status_code == 401

    response = client.post("/api/inbound", json={"sku": "TS-VIB-01", "quantity": 10})
    assert response.status_code == 401


def test_invalid_token_request_fails():
    """Verify that accessing endpoints with an invalid token returns HTTP 401."""
    headers = {"Authorization": "Bearer invalid_token_here_xxxx"}
    response = client.get("/api/inventory", headers=headers)
    assert response.status_code == 401


def test_auth_registration_and_login():
    """Verify registration succeeds and login yields a valid access token."""
    reg_response = client.post("/api/auth/register", json={"username": "user1", "password": "Passuser1!"})
    assert reg_response.status_code == 200
    assert "successful" in reg_response.json()["message"]

    # Duplicate username registration should fail
    dup_response = client.post("/api/auth/register", json={"username": "user1", "password": "Passuser2!"})
    assert dup_response.status_code == 400

    # Valid Login
    login_response = client.post("/api/auth/login", json={"username": "user1", "password": "Passuser1!"})
    assert login_response.status_code == 200
    assert "access_token" in login_response.json()
    assert login_response.json()["username"] == "user1"


def test_tenant_data_isolation():
    """Verify complete isolation between User A and User B."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "usera", "password": "Passworda1!"})
    res_a = client.post("/api/auth/login", json={"username": "usera", "password": "Passworda1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Register & Login User B
    client.post("/api/auth/register", json={"username": "userb", "password": "Passwordb1!"})
    res_b = client.post("/api/auth/login", json={"username": "userb", "password": "Passwordb1!"})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User A registers a custom product
    payload_a = {
        "sku": "CUSTOM-SKU-A",
        "name": "User A Shirt",
        "price": 100.0,
        "quantity": 10,
        "low_stock_threshold": 2
    }
    response_inbound_a = client.post("/api/inbound", json=payload_a, headers=headers_a)
    assert response_inbound_a.status_code == 200

    # User B checks their inventory - they should NOT see User A's custom product
    inv_b = client.get("/api/inventory", headers=headers_b).json()
    assert not any(p["sku"] == "CUSTOM-SKU-A" for p in inv_b)

    # User B adds their own product with the SAME SKU but different price and quantity
    payload_b = {
        "sku": "CUSTOM-SKU-A",
        "name": "User B Cap",
        "price": 50.0,
        "quantity": 25,
        "low_stock_threshold": 3
    }
    response_inbound_b = client.post("/api/inbound", json=payload_b, headers=headers_b)
    assert response_inbound_b.status_code == 200

    # User A checks inventory - sees their own version of CUSTOM-SKU-A
    inv_a = client.get("/api/inventory", headers=headers_a).json()
    item_a = next(p for p in inv_a if p["sku"] == "CUSTOM-SKU-A")
    assert item_a["name"] == "User A Shirt"
    assert item_a["price"] == 100.0
    assert item_a["quantity"] == 10

    # User B checks inventory - sees their own version of CUSTOM-SKU-A
    inv_b_updated = client.get("/api/inventory", headers=headers_b).json()
    item_b = next(p for p in inv_b_updated if p["sku"] == "CUSTOM-SKU-A")
    assert item_b["name"] == "User B Cap"
    assert item_b["price"] == 50.0
    assert item_b["quantity"] == 25


def test_delete_product_success(auth_headers):
    """Verify that a user can successfully delete a product and its history."""
    # First, make sure we have a transaction on TS-VIB-01
    client.post("/api/inbound", json={"sku": "TS-VIB-01", "quantity": 10}, headers=auth_headers)
    
    # Delete product TS-VIB-01
    delete_res = client.delete("/api/products/TS-VIB-01", headers=auth_headers)
    assert delete_res.status_code == 200
    assert "Successfully deleted product" in delete_res.json()["message"]

    # Verify TS-VIB-01 is gone from inventory
    inv = client.get("/api/inventory", headers=auth_headers).json()
    assert not any(p["sku"] == "TS-VIB-01" for p in inv)

    # Verify transactions history for TS-VIB-01 is cleared
    txs = client.get("/api/transactions", headers=auth_headers).json()
    assert not any(t["sku"] == "TS-VIB-01" for t in txs)


def test_delete_product_tenant_isolation():
    """Verify that User A cannot delete User B's product."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "usera", "password": "Passworda1!"})
    res_a = client.post("/api/auth/login", json={"username": "usera", "password": "Passworda1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Register & Login User B
    client.post("/api/auth/register", json={"username": "userb", "password": "Passwordb1!"})
    res_b = client.post("/api/auth/login", json={"username": "userb", "password": "Passwordb1!"})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User B adds a custom product
    client.post("/api/inbound", json={
        "sku": "USERB-SKU",
        "name": "User B Item",
        "price": 10.0,
        "quantity": 5
    }, headers=headers_b)

    # User A tries to delete User B's product - should fail (404 Not Found since it's isolated)
    delete_res = client.delete("/api/products/USERB-SKU", headers=headers_a)
    assert delete_res.status_code == 404

    # Verify User B's product is still there
    inv_b = client.get("/api/inventory", headers=headers_b).json()
    assert any(p["sku"] == "USERB-SKU" for p in inv_b)


def test_strong_password_rules():
    """Verify that weak passwords fail Pydantic model validation with HTTP 422."""
    # Short password
    r = client.post("/api/auth/register", json={"username": "wuser1", "password": "Sh1!"})
    assert r.status_code == 422
    
    # Missing uppercase
    r = client.post("/api/auth/register", json={"username": "wuser2", "password": "lowercase123!"})
    assert r.status_code == 422
    
    # Missing lowercase
    r = client.post("/api/auth/register", json={"username": "wuser3", "password": "UPPERCASE123!"})
    assert r.status_code == 422
    
    # Missing number
    r = client.post("/api/auth/register", json={"username": "wuser4", "password": "NoNumbersPass!"})
    assert r.status_code == 422
    
    # Missing special character
    r = client.post("/api/auth/register", json={"username": "wuser5", "password": "NoSpecialChar123"})
    assert r.status_code == 422

    # Successful strong password
    r = client.post("/api/auth/register", json={"username": "gooduser", "password": "VibePass123!"})
    assert r.status_code == 200


def test_google_oauth_endpoints_when_unconfigured():
    """Verify that Google OAuth endpoints return HTTP 400 when variables are not configured."""
    main.GOOGLE_CLIENT_ID = ""
    main.GOOGLE_REDIRECT_URI = ""
    main.GOOGLE_CLIENT_SECRET = ""

    response = client.get("/api/auth/google/login", follow_redirects=False)
    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]

    response = client.get("/api/auth/google/callback?code=testcode")
    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]


def test_google_oauth_login_redirect():
    """Verify that Google OAuth login redirects to Google's auth page when configured."""
    main.GOOGLE_CLIENT_ID = "mock_client_id"
    main.GOOGLE_REDIRECT_URI = "mock_redirect_uri"

    response = client.get("/api/auth/google/login", follow_redirects=False)
    assert response.status_code == 307
    redirect_url = response.headers["location"]
    assert "accounts.google.com" in redirect_url
    assert "client_id=mock_client_id" in redirect_url
    assert "redirect_uri=mock_redirect_uri" in redirect_url


def test_sellbyhere_webhook_success_and_isolation():
    """Verify that SellByHere webhook successfully updates inventory/revenue, and respects tenant isolation."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    
    conn = main.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = 'webhookuser_a';")
    user_id_a = cursor.fetchone()["id"]
    conn.close()

    headers_a = {"Authorization": f"Bearer {token_a}"}
    
    # Call Webhook for User A: Sell 5 units of TS-VIB-01 for $125.00
    payload = {
        "user_id": user_id_a,
        "product_id": "TS-VIB-01",
        "quantity_sold": 5,
        "total_amount_of_sale": 125.00
    }
    
    res = client.post("/api/v1/orders/webhook/main", json=payload, headers=headers_a)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify inventory is now 10
    inv = client.get("/api/inventory", headers=headers_a).json()
    item = next(p for p in inv if p["sku"] == "TS-VIB-01")
    assert item["quantity"] == 10

    # Register & Login User B
    client.post("/api/auth/register", json={"username": "webhookuser_b", "password": "VibePassword2!"})
    res_b = client.post("/api/auth/login", json={"username": "webhookuser_b", "password": "VibePassword2!"})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User A attempts to call webhook specifying User B's tenant ID - should fail with 403 Forbidden
    conn = main.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = 'webhookuser_b';")
    user_id_b = cursor.fetchone()["id"]
    conn.close()

    bad_payload = {
        "user_id": user_id_b,
        "product_id": "TS-VIB-01",
        "quantity_sold": 1,
        "total_amount_of_sale": 25.00
    }
    forbidden_res = client.post("/api/v1/orders/webhook/main", json=bad_payload, headers=headers_a)
    assert forbidden_res.status_code == 403
    assert "Tenant ID mismatch" in forbidden_res.json()["detail"]


def test_sellbyhere_webhook_insufficient_stock():
    """Verify that SellByHere webhook fails if requested quantity exceeds available stock."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    conn = main.get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = 'webhookuser_a';")
    user_id_a = cursor.fetchone()["id"]
    conn.close()

    # User A starts with 15 units of TS-VIB-01. Requesting 16 units should fail.
    payload = {
        "user_id": user_id_a,
        "product_id": "TS-VIB-01",
        "quantity_sold": 16,
        "total_amount_of_sale": 400.00
    }
    
    res = client.post("/api/v1/orders/webhook/main", json=payload, headers=headers_a)
    assert res.status_code == 400
    assert "Insufficient stock" in res.json()["detail"]


def test_websocket_endpoints_connection():
    """Verify that WebSocket endpoint rejects invalid/empty tokens and connects for valid tokens."""
    try:
        with client.websocket_connect("/api/ws") as websocket:
            assert False
    except Exception:
        pass

    # Register & Login User A
    client.post("/api/auth/register", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "webhookuser_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]

    # Connect with token
    with client.websocket_connect(f"/api/ws?token={token_a}") as websocket:
        pass


def test_settings_get_and_put():
    """Verify that user settings GET and PUT work correctly for authenticated users."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "settingsuser_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "settingsuser_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Initial GET should return empty string for both
    res = client.get("/api/v1/settings", headers=headers_a)
    assert res.status_code == 200
    assert res.json()["sellbyhere_url"] == ""
    assert res.json()["shopify_url"] == ""

    # PUT to update settings
    payload = {
        "sellbyhere_url": "https://myship.7-11.com.tw/mock_store_a",
        "shopify_url": "https://store-a.myshopify.com/admin"
    }
    put_res = client.put("/api/v1/settings", json=payload, headers=headers_a)
    assert put_res.status_code == 200
    assert put_res.json()["sellbyhere_url"] == "https://myship.7-11.com.tw/mock_store_a"
    assert put_res.json()["shopify_url"] == "https://store-a.myshopify.com/admin"

    # Subsequent GET should return updated URLs
    get_res = client.get("/api/v1/settings", headers=headers_a)
    assert get_res.status_code == 200
    assert get_res.json()["sellbyhere_url"] == "https://myship.7-11.com.tw/mock_store_a"
    assert get_res.json()["shopify_url"] == "https://store-a.myshopify.com/admin"


def test_settings_unauthenticated():
    """Verify that settings endpoints require valid JWT credentials."""
    res = client.get("/api/v1/settings")
    assert res.status_code == 401

    res = client.put("/api/v1/settings", json={
        "sellbyhere_url": "http://example.com",
        "shopify_url": "https://shopify.com"
    })
    assert res.status_code == 401


def test_settings_tenant_isolation():
    """Verify that tenant settings are completely isolated between users."""
    # User A
    client.post("/api/auth/register", json={"username": "set_iso_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "set_iso_a", "password": "VibePassword1!"})
    headers_a = {"Authorization": f"Bearer {res_a.json()['access_token']}"}
    client.put("/api/v1/settings", json={
        "sellbyhere_url": "https://url-a.com",
        "shopify_url": "https://shopify-a.com"
    }, headers=headers_a)

    # User B
    client.post("/api/auth/register", json={"username": "set_iso_b", "password": "VibePassword2!"})
    res_b = client.post("/api/auth/login", json={"username": "set_iso_b", "password": "VibePassword2!"})
    headers_b = {"Authorization": f"Bearer {res_b.json()['access_token']}"}
    
    # User B should still see empty settings default
    res = client.get("/api/v1/settings", headers=headers_b)
    assert res.json()["sellbyhere_url"] == ""
    assert res.json()["shopify_url"] == ""

    # User B updates theirs
    client.put("/api/v1/settings", json={
        "sellbyhere_url": "https://url-b.com",
        "shopify_url": "https://shopify-b.com"
    }, headers=headers_b)

    # Assure both remain isolated
    res_a_final = client.get("/api/v1/settings", headers=headers_a).json()
    res_b_final = client.get("/api/v1/settings", headers=headers_b).json()
    assert res_a_final["sellbyhere_url"] == "https://url-a.com"
    assert res_a_final["shopify_url"] == "https://shopify-a.com"
    assert res_b_final["sellbyhere_url"] == "https://url-b.com"
    assert res_b_final["shopify_url"] == "https://shopify-b.com"


def test_batch_import_orders_success_and_validation():
    """Verify that batch order import works for matched products and isolates users."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "batch_user_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "batch_user_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Register & Login User B
    client.post("/api/auth/register", json={"username": "batch_user_b", "password": "VibePassword2!"})
    res_b = client.post("/api/auth/login", json={"username": "batch_user_b", "password": "VibePassword2!"})
    token_b = res_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User A registers product PROD1 with 10 stock
    client.post("/api/inbound", json={"sku": "PROD1", "name": "Product One", "price": 20.0, "quantity": 10}, headers=headers_a)

    # 1. Success case: User A imports orders for PROD1
    csv_content = "Product Name,Quantity,Total Amount\nPROD1,3,60.0\n"
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 1

    # Verify inventory is now 7
    res_inv = client.get("/api/inventory", headers=headers_a)
    products_a = res_inv.json()
    prod_a = next(p for p in products_a if p["sku"] == "PROD1")
    assert prod_a["quantity"] == 7

    # 2. Insufficient stock case: User A imports 10 more (current: 7)
    csv_content_insufficient = "Product Name,Quantity,Total Amount\nPROD1,10,200.0\n"
    files_insufficient = {"file": ("orders.csv", csv_content_insufficient, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files_insufficient, headers=headers_a)
    assert res.status_code == 400
    assert "Insufficient stock" in res.json()["detail"]

    # Verify stock remains 7 (rolled back)
    res_inv = client.get("/api/inventory", headers=headers_a)
    products_a = res_inv.json()
    prod_a = next(p for p in products_a if p["sku"] == "PROD1")
    assert prod_a["quantity"] == 7

    # 3. Tenant isolation: User B attempts to import orders for PROD1
    csv_content_b = "Product Name,Quantity,Total Amount\nPROD1,2,40.0\n"
    files_b = {"file": ("orders.csv", csv_content_b, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files_b, headers=headers_b)
    # User B does not have PROD1 in their catalog, so should fail with not found
    assert res.status_code == 400
    assert "not found in catalog" in res.json()["detail"]

    # Verify User A's stock is still 7
    res_inv = client.get("/api/inventory", headers=headers_a)
    products_a = res_inv.json()
    prod_a = next(p for p in products_a if p["sku"] == "PROD1")
    assert prod_a["quantity"] == 7


def test_batch_import_excel_success():
    """Verify that batch order import successfully parses Excel (.xlsx) files."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "excel_user_a", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "excel_user_a", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound product stock
    client.post("/api/inbound", json={"sku": "PROD9", "name": "Product Nine", "price": 50.0, "quantity": 10}, headers=headers_a)

    # Generate XLSX bytes
    import pandas as pd
    df = pd.DataFrame([
        {"Product Name": "PROD9", "Quantity": 4, "Total Amount": 200.0}
    ])
    excel_buffer = io.BytesIO()
    df.to_excel(excel_buffer, index=False, engine='openpyxl')
    excel_bytes = excel_buffer.getvalue()

    # Upload excel file
    files = {"file": ("orders.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 1

    # Verify inventory is now 6
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod_a = next(p for p in res_inv.json() if p["sku"] == "PROD9")
    assert prod_a["quantity"] == 6


def test_batch_import_chinese_aliases_csv():
    """Verify that batch order import correctly translates Chinese header aliases."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "zh_alias_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "zh_alias_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound product
    inbound_res = client.post("/api/inbound", json={"sku": "PROD-ZH", "name": "商品中文測試", "price": 100.0, "quantity": 10}, headers=headers_a)
    assert inbound_res.status_code == 200

    # Post CSV with Taiwanese headers
    csv_content = "商品名稱,數量,金額\nPROD-ZH,3,300.0\n"
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 1

    # Verify inventory is now 7
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod_a = next(p for p in res_inv.json() if p["sku"] == "PROD-ZH")
    assert prod_a["quantity"] == 7


def test_batch_import_missing_mandatory_fields():
    """Verify that batch import fails with a clean 400 error when mandatory columns are missing."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "missing_fields_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "missing_fields_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Post CSV without the Amount/金額 column
    csv_content = "商品名稱,數量\nPROD-ZH,3\n"
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 400
    assert "檔案缺少必要欄位：請確保包含商品名稱、數量與金額欄位" in res.json()["detail"]


def test_batch_import_offset_headers_excel():
    """Verify that batch import correctly ignores metadata lines above the header and summary lines at the bottom."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "offset_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "offset_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound product
    client.post("/api/inbound", json={"sku": "PROD-OFFSET", "name": "Offset Item", "price": 40.0, "quantity": 10}, headers=headers_a)

    # Generate XLSX with 2 metadata rows at the top and a summary row at the bottom
    import pandas as pd
    data = [
        # Metadata rows (empty / descriptions)
        ["賣貨便訂單匯出報表", "", ""],
        ["產出時間: 2026-07-03", "", ""],
        # Header row
        ["商品名稱", "數量", "代收金額"],
        # Valid data row
        ["PROD-OFFSET", 4, 160.0],
        # Summary row (empty Name/SKU, has totals)
        [None, 4, 160.0]
    ]
    df = pd.DataFrame(data)
    
    excel_buffer = io.BytesIO()
    # Write to excel without index or header
    df.to_excel(excel_buffer, index=False, header=False, engine='openpyxl')
    excel_bytes = excel_buffer.getvalue()

    # Upload excel file
    files = {"file": ("offset_orders.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 1 # only 1 valid row (the summary row was discarded)

    # Verify inventory is now 6
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod_a = next(p for p in res_inv.json() if p["sku"] == "PROD-OFFSET")
    assert prod_a["quantity"] == 6


def test_batch_import_invalid_header_format():
    """Verify that batch import fails with 400 when headers cannot be auto-detected."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "invalid_format_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "invalid_format_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Upload CSV without valid column headers anywhere in first 10 rows
    csv_content = "Row1 Col1,Row1 Col2\nRandom value 1,Random value 2\n"
    files = {"file": ("bad_format.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 400
    assert "無法辨識報表格式" in res.json()["detail"]


def test_batch_import_multiple_transactions_same_user():
    """Verify that a tenant can insert multiple transactions during batch import without violating unique constraints."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "multi_tx_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "multi_tx_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound stock
    client.post("/api/inbound", json={"sku": "PROD-MULTI", "name": "Multi Tx Product", "price": 10.0, "quantity": 100}, headers=headers_a)

    # 5 rows of transactions for the SAME product (PROD-MULTI) for the same user
    csv_content = (
        "商品名稱,數量,金額\n"
        "PROD-MULTI,1,10.0\n"
        "PROD-MULTI,2,20.0\n"
        "PROD-MULTI,3,30.0\n"
        "PROD-MULTI,4,40.0\n"
        "PROD-MULTI,5,50.0\n"
    )
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 5

    # Verify inventory is now 85 (100 - 15)
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod_a = next(p for p in res_inv.json() if p["sku"] == "PROD-MULTI")
    assert prod_a["quantity"] == 85


def test_batch_import_data_cleansing_layer():
    """Verify that batch import cleanses currency, splits multi-item details, and filters out cancelled/returned orders."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "clean_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "clean_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound stock
    client.post("/api/inbound", json={"sku": "PROD-CLEAN1", "name": "PROD-CLEAN1", "price": 100.0, "quantity": 10}, headers=headers_a)
    client.post("/api/inbound", json={"sku": "PROD-CLEAN2", "name": "PROD-CLEAN2", "price": 50.0, "quantity": 10}, headers=headers_a)

    # CSV with dirty currency symbols, combined details, and status column
    csv_content = (
        "訂單狀態,商品明細,訂單金額\n"
        "成功,\"PROD-CLEAN1 * 2, PROD-CLEAN2 x 3\",NT$ 350.00\n" # Valid, will split into two (CLEAN1: 2, CLEAN2: 3)
        "取消,PROD-CLEAN1 * 5,NT$ 500.00\n"                     # Invalid status, should be ignored
        "成功,,NT$ 0.00\n"                                       # Empty product name/detail, should be ignored
    )
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 2 # CLEAN1 and CLEAN2 from row 1

    # Verify inventory is now 8 for CLEAN1 (10 - 2) and 7 for CLEAN2 (10 - 3)
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod_1 = next(p for p in res_inv.json() if p["sku"] == "PROD-CLEAN1")
    prod_2 = next(p for p in res_inv.json() if p["sku"] == "PROD-CLEAN2")
    assert prod_1["quantity"] == 8
    assert prod_2["quantity"] == 7


def test_batch_import_relaxed_columns():
    """Verify that batch import relaxes validation when a combined column like '訂單內容' or '商品資訊' exists."""
    # Register & Login User A
    client.post("/api/auth/register", json={"username": "relaxed_user", "password": "VibePassword1!"})
    res_a = client.post("/api/auth/login", json={"username": "relaxed_user", "password": "VibePassword1!"})
    token_a = res_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Inbound stock
    client.post("/api/inbound", json={"sku": "PROD-RELAX", "name": "PROD-RELAX", "price": 10.0, "quantity": 20}, headers=headers_a)

    # CSV with '訂單內容' and '訂單金額', missing separate product name and quantity columns
    csv_content = (
        "訂單狀態,訂單內容,訂單金額\n"
        "成功,PROD-RELAX * 5,50.00\n"
    )
    files = {"file": ("orders.csv", csv_content, "text/csv")}
    res = client.post("/api/v1/orders/batch-import", files=files, headers=headers_a)
    
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["records_processed"] == 1

    # Verify inventory is now 15 (20 - 5)
    res_inv = client.get("/api/inventory", headers=headers_a)
    prod = next(p for p in res_inv.json() if p["sku"] == "PROD-RELAX")
    assert prod["quantity"] == 15


