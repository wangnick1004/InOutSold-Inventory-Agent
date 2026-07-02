import sqlite3
import os
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, status, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
import jwt
import bcrypt
import httpx

# SQLite Database File
DB_FILE = "vibe_inventory.db"

# JWT configuration
JWT_SECRET = "super-secret-key-change-in-prod-1007524228578"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# Load .env file if it exists (for local development)
if os.path.exists(".env"):
    try:
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        pass

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")

def get_password_hash(password: str) -> str:
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    pwd_bytes = plain_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)

# Security Sanitization Helpers
def sanitize_sku(sku: str) -> str:
    if not sku:
        raise HTTPException(status_code=400, detail="SKU cannot be empty.")
    clean = sku.strip().upper()
    # Enforce safe format to prevent SQL injection attempts or path injections
    if not re.match(r"^[A-Z0-9\-]+$", clean):
        raise HTTPException(status_code=400, detail="Invalid SKU format. Only alphanumeric characters and hyphens allowed.")
    if len(clean) > 50:
        raise HTTPException(status_code=400, detail="SKU length cannot exceed 50 characters.")
    return clean

def sanitize_name(name: str) -> str:
    if not name:
        raise HTTPException(status_code=400, detail="Product name cannot be empty.")
    clean = name.strip()
    # Strip script tags along with their inner code block content
    clean = re.sub(r"<script\b[^>]*>([\s\S]*?)<\/script>", "", clean, flags=re.IGNORECASE)
    # Strip any other remaining HTML tags
    clean = re.sub(r"<[^>]*>", "", clean)
    if not clean.strip():
        raise HTTPException(status_code=400, detail="Product name cannot be blank or contain only HTML tags.")
    if len(clean) > 100:
        raise HTTPException(status_code=400, detail="Product name length cannot exceed 100 characters.")
    return clean

app = FastAPI(title="InOutSold Backend", version="2.0.0")

# Setup CORS to allow local HTML files to make requests to localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
)

# Route to serve the frontend single-page dashboard directly
@app.get("/", response_class=HTMLResponse)
def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="index.html not found in workspace.")

# Helper: Connect to the SQLite Database
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys support in SQLite
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

# Helper: Initialize database with multi-tenant architecture
def init_db(force_reset: bool = False):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Detect old schema: check if 'users' table exists and products has user_id
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
    has_users_table = cursor.fetchone() is not None

    has_user_id_column = False
    if has_users_table:
        cursor.execute("PRAGMA table_info(products);")
        columns = [c[1] for c in cursor.fetchall()]
        has_user_id_column = "user_id" in columns

    # If force_reset or old schema is detected, drop tables and rebuild
    if force_reset or not has_users_table or not has_user_id_column:
        cursor.execute("DROP TABLE IF EXISTS transactions;")
        cursor.execute("DROP TABLE IF EXISTS inventory_counts;")
        cursor.execute("DROP TABLE IF EXISTS products;")
        cursor.execute("DROP TABLE IF EXISTS users;")

    # 1. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    );
    """)

    # 2. Products Table (Composite Key to allow different users to define the same SKU)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        sku TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price REAL NOT NULL CHECK(price >= 0),
        low_stock_threshold INTEGER NOT NULL DEFAULT 5 CHECK(low_stock_threshold >= 0),
        PRIMARY KEY (sku, user_id),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # 3. Inventory Counts Table (Physical quantities isolated by SKU and User)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS inventory_counts (
        sku TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
        PRIMARY KEY (sku, user_id),
        FOREIGN KEY (sku, user_id) REFERENCES products(sku, user_id) ON DELETE CASCADE
    );
    """)

    # 4. Transactions Table (Isolated Audit Trail)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        type TEXT CHECK(type IN ('INBOUND', 'OUTBOUND')) NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity > 0),
        price REAL NOT NULL CHECK(price >= 0),
        revenue REAL NOT NULL DEFAULT 0.0,
        transaction_date TEXT NOT NULL,
        FOREIGN KEY (sku, user_id) REFERENCES products(sku, user_id) ON DELETE CASCADE
    );
    """)

    # Seed Default User 'admin' / 'admin123' if table is empty
    cursor.execute("SELECT COUNT(*) FROM users;")
    if cursor.fetchone()[0] == 0:
        admin_pass_hash = get_password_hash("admin123")
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?);",
            ("admin", admin_pass_hash)
        )
        admin_id = cursor.lastrowid

        # Seed defaults for admin
        default_products = [
            ("TS-VIB-01", "Vibe Shirt", 25.00, 5, 15),
            ("CP-RET-02", "Retro Cap", 15.00, 5, 3),
            ("MG-NEO-03", "Neon Mug", 12.00, 2, 0)
        ]
        
        for sku, name, price, threshold, qty in default_products:
            cursor.execute(
                "INSERT INTO products (sku, user_id, name, price, low_stock_threshold) VALUES (?, ?, ?, ?, ?);",
                (sku, admin_id, name, price, threshold)
            )
            cursor.execute(
                "INSERT INTO inventory_counts (sku, user_id, quantity) VALUES (?, ?, ?);",
                (sku, admin_id, qty)
            )
            
            if qty > 0:
                cursor.execute(
                    "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (sku, admin_id, "INBOUND", qty, price, 0.0, datetime.now().isoformat())
                )

    conn.commit()
    conn.close()

# Initialize DB on Startup
@app.on_event("startup")
def startup_event():
    init_db()

# --- Auth Helpers ---
def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token content: sub field is missing."
            )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token."
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username FROM users WHERE username = ?;", (username,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user profile not found in database."
        )

    return {"id": row["id"], "username": row["username"]}

# --- Pydantic Data Models ---
class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=4, max_length=100)

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=100)

    @field_validator('password')
    @classmethod
    def validate_strong_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one number.")
        if not re.search(r"[@$!%*?&]", v):
            raise ValueError("Password must contain at least one special character (@$!%*?&).")
        return v

class ProductResponse(BaseModel):
    sku: str
    name: str
    price: float
    low_stock_threshold: int
    quantity: int
    status: str

class InboundRequest(BaseModel):
    sku: str = Field(..., description="Unique product SKU identifier")
    quantity: int = Field(..., gt=0, description="Quantity of stock to add")
    name: Optional[str] = Field(None, description="Product Name (required for new SKUs)")
    price: Optional[float] = Field(None, ge=0.0, description="Unit Price (required for new SKUs)")
    low_stock_threshold: Optional[int] = Field(5, ge=0, description="Alert threshold for low stock")

class OutboundRequest(BaseModel):
    sku: str = Field(..., description="Product SKU to check out")
    quantity: int = Field(..., gt=0, description="Quantity to purchase")
    price: Optional[float] = Field(None, ge=0.0, description="Override unit price.")

class TransactionResponse(BaseModel):
    id: int
    sku: str
    type: str
    quantity: int
    price: float
    revenue: float
    transaction_date: str

class KPIResponse(BaseModel):
    total_products: int
    total_stock_items: int
    low_stock_alerts: int
    total_revenue: float

# --- Authentication Routes ---
@app.post("/api/auth/register")
def register_user(req: RegisterRequest):
    username = req.username.strip()
    if not re.match(r"^[a-zA-Z0-9_\-]+$", username):
        raise HTTPException(
            status_code=400,
            detail="Invalid username format. Only letters, numbers, underscores, and hyphens allowed."
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE username = ?;", (username,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Username already exists.")

        hashed_pass = get_password_hash(req.password)
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?);", (username, hashed_pass))
        user_id = cursor.lastrowid

        # Seed new user with starting catalog defaults so dashboard isn't blank
        default_products = [
            ("TS-VIB-01", "Vibe Shirt", 25.00, 5, 15),
            ("CP-RET-02", "Retro Cap", 15.00, 5, 3),
            ("MG-NEO-03", "Neon Mug", 12.00, 2, 0)
        ]
        for sku, name, price, threshold, qty in default_products:
            cursor.execute(
                "INSERT INTO products (sku, user_id, name, price, low_stock_threshold) VALUES (?, ?, ?, ?, ?);",
                (sku, user_id, name, price, threshold)
            )
            cursor.execute(
                "INSERT INTO inventory_counts (sku, user_id, quantity) VALUES (?, ?, ?);",
                (sku, user_id, qty)
            )
            if qty > 0:
                cursor.execute(
                    "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (sku, user_id, "INBOUND", qty, price, 0.0, datetime.now().isoformat())
                )

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during registration: {str(e)}")
    finally:
        conn.close()

    return {"message": "Registration successful."}

@app.post("/api/auth/login")
def login_user(req: AuthRequest):
    username = req.username.strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash FROM users WHERE username = ?;", (username,))
    row = cursor.fetchone()
    conn.close()

    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password.")

    token = create_access_token({"sub": username})
    return {"access_token": token, "token_type": "bearer", "username": username}

# --- Google OAuth Routes ---

@app.get("/api/auth/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(
            status_code=400,
            detail="Google OAuth is not configured. Missing Client ID or Redirect URI."
        )
    # Redirect to Google OAuth authorization endpoint
    google_auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=openid%20email%20profile"
    )
    return RedirectResponse(google_auth_url)

@app.get("/api/auth/google/callback")
async def google_callback(code: str):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise HTTPException(
            status_code=400,
            detail="Google OAuth is not configured. Missing credentials."
        )

    # Exchange authorization code for token
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            }
        )
        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to retrieve Google token: {token_response.text}"
            )
        
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=400,
                detail="Token exchange response did not return an access token."
            )

        # Retrieve user profile
        userinfo_response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to retrieve Google user profile: {userinfo_response.text}"
            )
        
        user_info = userinfo_response.json()
        email = user_info.get("email")
        if not email:
            raise HTTPException(
                status_code=400,
                detail="Google profile did not contain an email address."
            )

    username = email.strip()
    
    # Check if user exists in SQLite 'users' table, auto-register if not
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM users WHERE username = ?;", (username,))
        user_row = cursor.fetchone()
        
        if not user_row:
            # Auto-register this user: hash a dummy password
            dummy_hash = get_password_hash("oauth-auto-generated-pw-" + os.urandom(8).hex())
            cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?);", (username, dummy_hash))
            user_id = cursor.lastrowid
            
            # Seed new user with starting catalog defaults so dashboard isn't blank
            default_products = [
                ("TS-VIB-01", "Vibe Shirt", 25.00, 5, 15),
                ("CP-RET-02", "Retro Cap", 15.00, 5, 3),
                ("MG-NEO-03", "Neon Mug", 12.00, 2, 0)
            ]
            for sku, name, price, threshold, qty in default_products:
                cursor.execute(
                    "INSERT INTO products (sku, user_id, name, price, low_stock_threshold) VALUES (?, ?, ?, ?, ?);",
                    (sku, user_id, name, price, threshold)
                )
                cursor.execute(
                    "INSERT INTO inventory_counts (sku, user_id, quantity) VALUES (?, ?, ?);",
                    (sku, user_id, qty)
                )
                if qty > 0:
                    cursor.execute(
                        "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
                        (sku, user_id, "INBOUND", qty, price, 0.0, datetime.now().isoformat())
                    )
            conn.commit()
            
        token = create_access_token({"sub": username})
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database error during Google OAuth callback processing: {str(e)}"
        )
    finally:
        conn.close()

    # Redirect to index page with JWT appended as a query parameter
    return RedirectResponse(url=f"/?token={token}&username={username}")

# --- Isolated Core API Endpoints ---
@app.get("/api/inventory", response_model=List[ProductResponse])
def get_inventory(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.sku, p.name, p.price, p.low_stock_threshold, COALESCE(i.quantity, 0) AS quantity
        FROM products p
        LEFT JOIN inventory_counts i ON p.sku = i.sku AND p.user_id = i.user_id
        WHERE p.user_id = ?;
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        qty = r["quantity"]
        thresh = r["low_stock_threshold"]
        
        if qty == 0:
            status_str = "Out of Stock"
        elif qty <= thresh:
            status_str = "Low Stock"
        else:
            status_str = "In Stock"

        result.append(ProductResponse(
            sku=r["sku"],
            name=r["name"],
            price=r["price"],
            low_stock_threshold=thresh,
            quantity=qty,
            status=status_str
        ))
    return result

@app.get("/api/kpis", response_model=KPIResponse)
def get_kpis(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Total products for user
    cursor.execute("SELECT COUNT(*) FROM products WHERE user_id = ?;", (user_id,))
    total_products = cursor.fetchone()[0]

    # 2. Total stock items for user
    cursor.execute("SELECT COALESCE(SUM(quantity), 0) FROM inventory_counts WHERE user_id = ?;", (user_id,))
    total_stock_items = cursor.fetchone()[0]

    # 3. Low stock alerts for user (< 5 units)
    cursor.execute("SELECT COUNT(*) FROM inventory_counts WHERE user_id = ? AND quantity < 5;", (user_id,))
    low_stock_alerts = cursor.fetchone()[0]

    # 4. Total revenue from OUTBOUND transactions for user
    cursor.execute("SELECT COALESCE(SUM(revenue), 0.0) FROM transactions WHERE user_id = ? AND type = 'OUTBOUND';", (user_id,))
    total_revenue = cursor.fetchone()[0]

    conn.close()

    return KPIResponse(
        total_products=total_products,
        total_stock_items=total_stock_items,
        low_stock_alerts=low_stock_alerts,
        total_revenue=total_revenue
    )

@app.get("/api/transactions", response_model=List[TransactionResponse])
def get_transactions(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, sku, type, quantity, price, revenue, transaction_date
        FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 5;
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    return [TransactionResponse(**dict(r)) for r in rows]

@app.post("/api/inbound")
def add_inbound(req: InboundRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    # Validation checks
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero.")
    if req.price is not None and req.price < 0:
        raise HTTPException(status_code=400, detail="Price cannot be negative.")
    if req.low_stock_threshold is not None and req.low_stock_threshold < 0:
        raise HTTPException(status_code=400, detail="Low stock threshold cannot be negative.")

    sku = sanitize_sku(req.sku)
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name, price FROM products WHERE sku = ? AND user_id = ?;", (sku, user_id))
        prod = cursor.fetchone()

        if prod:
            # Existing SKU: Add stock
            cursor.execute(
                "UPDATE inventory_counts SET quantity = quantity + ? WHERE sku = ? AND user_id = ?;",
                (req.quantity, sku, user_id)
            )
            price = prod["price"]
        else:
            # New SKU: Register product first
            if not req.name or req.price is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="SKU does not exist. Product name and price are required to register it."
                )

            clean_name = sanitize_name(req.name)
            cursor.execute(
                "INSERT INTO products (sku, user_id, name, price, low_stock_threshold) VALUES (?, ?, ?, ?, ?);",
                (sku, user_id, clean_name, req.price, req.low_stock_threshold)
            )
            cursor.execute(
                "INSERT INTO inventory_counts (sku, user_id, quantity) VALUES (?, ?, ?);",
                (sku, user_id, req.quantity)
            )
            price = req.price

        # Record INBOUND Transaction
        cursor.execute(
            "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
            (sku, user_id, "INBOUND", req.quantity, price, 0.0, datetime.now().isoformat())
        )

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction failed: {str(e)}"
        )
    finally:
        conn.close()

    return {"message": f"Successfully processed inbound stock of {req.quantity} units for SKU {sku}"}

@app.post("/api/outbound")
def process_outbound(req: OutboundRequest, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    # Validation checks
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero.")
    if req.price is not None and req.price < 0:
        raise HTTPException(status_code=400, detail="Price cannot be negative.")

    sku = sanitize_sku(req.sku)
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT p.name, p.price, COALESCE(i.quantity, 0) AS quantity
            FROM products p
            LEFT JOIN inventory_counts i ON p.sku = i.sku AND p.user_id = i.user_id
            WHERE p.sku = ? AND p.user_id = ?;
        """, (sku, user_id))
        prod = cursor.fetchone()

        if not prod:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product with SKU '{sku}' not found."
            )

        current_qty = prod["quantity"]
        product_name = prod["name"]
        unit_price = req.price if req.price is not None else prod["price"]

        # Validate stock availability
        if current_qty < req.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for {product_name} (Only {current_qty} remaining)"
            )

        # Deduct quantity
        cursor.execute(
            "UPDATE inventory_counts SET quantity = quantity - ? WHERE sku = ? AND user_id = ?;",
            (req.quantity, sku, user_id)
        )

        # Record OUTBOUND Transaction
        revenue = req.quantity * unit_price
        cursor.execute(
            "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
            (sku, user_id, "OUTBOUND", req.quantity, unit_price, revenue, datetime.now().isoformat())
        )

        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction failed: {str(e)}"
        )
    finally:
        conn.close()

    return {
        "message": f"Outbound sale processed successfully.",
        "product": product_name,
        "quantity": req.quantity,
        "revenue": revenue
    }

@app.post("/api/reset")
def reset_database(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Delete user's specific catalog data
        cursor.execute("DELETE FROM transactions WHERE user_id = ?;", (user_id,))
        cursor.execute("DELETE FROM inventory_counts WHERE user_id = ?;", (user_id,))
        cursor.execute("DELETE FROM products WHERE user_id = ?;", (user_id,))
        
        # Reseed catalog defaults for this specific user
        default_products = [
            ("TS-VIB-01", "Vibe Shirt", 25.00, 5, 15),
            ("CP-RET-02", "Retro Cap", 15.00, 5, 3),
            ("MG-NEO-03", "Neon Mug", 12.00, 2, 0)
        ]
        
        for sku, name, price, threshold, qty in default_products:
            cursor.execute(
                "INSERT INTO products (sku, user_id, name, price, low_stock_threshold) VALUES (?, ?, ?, ?, ?);",
                (sku, user_id, name, price, threshold)
            )
            cursor.execute(
                "INSERT INTO inventory_counts (sku, user_id, quantity) VALUES (?, ?, ?);",
                (sku, user_id, qty)
            )
            
            if qty > 0:
                cursor.execute(
                    "INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date) VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (sku, user_id, "INBOUND", qty, price, 0.0, datetime.now().isoformat())
                )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset user database: {str(e)}"
        )
    finally:
        conn.close()
    return {"message": "Your inventory database has been reset to defaults successfully."}

@app.delete("/api/products/{sku}")
def delete_product(sku: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    sku = sanitize_sku(sku)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Check if product exists for this user
        cursor.execute("SELECT sku FROM products WHERE sku = ? AND user_id = ?;", (sku, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"Product with SKU '{sku}' not found.")

        # Explicitly delete related records to avoid key violations
        cursor.execute("DELETE FROM transactions WHERE sku = ? AND user_id = ?;", (sku, user_id))
        cursor.execute("DELETE FROM inventory_counts WHERE sku = ? AND user_id = ?;", (sku, user_id))
        
        # Delete the product item
        cursor.execute("DELETE FROM products WHERE sku = ? AND user_id = ?;", (sku, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database transaction failed: {str(e)}"
        )
    finally:
        conn.close()

    return {"message": f"Successfully deleted product SKU '{sku}' and all associated history."}
