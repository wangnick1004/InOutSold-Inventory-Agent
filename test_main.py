import os
import pytest
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
