import sqlite3
import os
import re
import csv
import io
import pandas as pd
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, status, Depends, WebSocket, WebSocketDisconnect, File, UploadFile
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

    # Check if shopify_url column is missing from user_settings (for existing systems)
    if has_users_table:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings';")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(user_settings);")
            settings_cols = [c[1] for c in cursor.fetchall()]
            if "shopify_url" not in settings_cols:
                try:
                    cursor.execute("ALTER TABLE user_settings ADD COLUMN shopify_url TEXT NOT NULL DEFAULT '';")
                except sqlite3.Error:
                    pass

    # If force_reset or old schema is detected, drop tables and rebuild
    if force_reset or not has_users_table or not has_user_id_column:
        cursor.execute("DROP TABLE IF EXISTS user_settings;")
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

    # 5. User Settings Table (Isolated settings like store URLs)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        sellbyhere_url TEXT NOT NULL DEFAULT '',
        shopify_url TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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


# --- WebSocket Broadcasting Setup ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: int):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def broadcast_to_user(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

manager = ConnectionManager()

@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None):
    if not token:
        query_string = websocket.scope.get("query_string", b"").decode("utf-8")
        params = dict(x.split("=") for x in query_string.split("&") if "=" in x)
        token = params.get("token")

    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ?;", (username,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        user_id = row["id"]
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)


# --- SellByHere Order Webhook ---
class WebhookOrderPayload(BaseModel):
    user_id: int
    product_id: str
    quantity_sold: int
    total_amount_of_sale: float

@app.post("/api/v1/orders/webhook/main")
async def orders_webhook(payload: WebhookOrderPayload, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    if payload.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant ID mismatch. You cannot submit webhooks for another user's tenant."
        )

    sku = payload.product_id.strip()
    qty = payload.quantity_sold
    amount = payload.total_amount_of_sale

    if qty <= 0:
        raise HTTPException(status_code=400, detail="Quantity sold must be positive.")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Total amount of sale cannot be negative.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Check if product exists for this user
        cursor.execute("SELECT name, price FROM products WHERE sku = ? AND user_id = ?;", (sku, user_id))
        prod = cursor.fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail=f"Product with SKU '{sku}' not found for this tenant.")

        # Check stock levels
        cursor.execute("SELECT quantity FROM inventory_counts WHERE sku = ? AND user_id = ?;", (sku, user_id))
        stock = cursor.fetchone()
        current_qty = stock["quantity"] if stock else 0

        if current_qty < qty:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for SKU '{sku}'. Available: {current_qty}, Requested: {qty}")

        # Update stock
        new_qty = current_qty - qty
        cursor.execute(
            "UPDATE inventory_counts SET quantity = ? WHERE sku = ? AND user_id = ?;",
            (new_qty, sku, user_id)
        )

        # Record outbound transaction
        cursor.execute(
            """
            INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date)
            VALUES (?, ?, 'OUTBOUND', ?, ?, ?, ?);
            """,
            (sku, user_id, qty, amount / qty if qty > 0 else 0, amount, datetime.now().isoformat())
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error during webhook synchronization: {str(e)}")
    finally:
        conn.close()

    # Trigger websocket broadcast to update clients
    broadcast_data = {
        "event": "order_synced",
        "user_id": user_id,
        "data": {
            "sku": sku,
            "product_name": prod["name"],
            "quantity_sold": qty,
            "total_amount": amount,
            "new_stock": new_qty,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    }
    await manager.broadcast_to_user(user_id, broadcast_data)

    return {"status": "success", "message": "Order synchronized successfully."}


# --- Tenant Settings Endpoints ---
class SettingsPayload(BaseModel):
    sellbyhere_url: str = Field(default="", description="The URL of the user's external SellByHere store.")
    shopify_url: str = Field(default="", description="The URL of the user's external Shopify store.")

@app.get("/api/v1/settings")
def get_user_settings(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT sellbyhere_url, shopify_url FROM user_settings WHERE user_id = ?;", (user_id,))
    row = cursor.fetchone()
    conn.close()

    sellbyhere = row["sellbyhere_url"] if row else ""
    shopify = row["shopify_url"] if row else ""
    return {"sellbyhere_url": sellbyhere, "shopify_url": shopify}

@app.put("/api/v1/settings")
def update_user_settings(payload: SettingsPayload, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    sellbyhere = payload.sellbyhere_url.strip()
    shopify = payload.shopify_url.strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Check if settings record exists
        cursor.execute("SELECT user_id FROM user_settings WHERE user_id = ?;", (user_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE user_settings SET sellbyhere_url = ?, shopify_url = ? WHERE user_id = ?;",
                (sellbyhere, shopify, user_id)
            )
        else:
            cursor.execute(
                "INSERT INTO user_settings (user_id, sellbyhere_url, shopify_url) VALUES (?, ?, ?);",
                (user_id, sellbyhere, shopify)
            )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {str(e)}")
    finally:
        conn.close()

    return {
        "status": "success",
        "message": "Settings saved successfully.",
        "sellbyhere_url": sellbyhere,
        "shopify_url": shopify
    }


@app.post("/api/v1/orders/batch-import")
async def batch_import_orders(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    filename = file.filename.lower()
    is_csv = filename.endswith('.csv')
    is_excel = filename.endswith(('.xlsx', '.xls'))
    
    if not is_csv and not is_excel:
        raise HTTPException(status_code=400, detail="不支援的檔案格式，請上傳 .csv 或 .xlsx 檔案。")
        
    try:
        contents = await file.read()
        
        detected_encoding = 'utf-8'
        raw_df = None
        
        if is_csv:
            encodings = ['utf-8', 'big5', 'cp950', 'utf-8-sig', 'gbk']
            decoded_successfully = False
            
            for enc in encodings:
                try:
                    raw_df = pd.read_csv(io.BytesIO(contents), header=None, nrows=10, encoding=enc)
                    detected_encoding = enc
                    decoded_successfully = True
                    break
                except Exception:
                    continue
                    
            if not decoded_successfully:
                raise HTTPException(status_code=400, detail="檔案編碼不支援，請確保為標準 UTF-8 或 Big5 CSV 格式。")
        else:
            try:
                raw_df = pd.read_excel(io.BytesIO(contents), header=None, nrows=10, engine="openpyxl")
            except Exception:
                raise HTTPException(status_code=400, detail="無法解析 Excel 檔案，請確認檔案未損毀。")

        product_keywords = {'商品名稱', '品名', '商品明細', '商品', 'product name', 'sku', 'product_name', 'name', '訂單內容', '商品資訊'}
        quantity_keywords = {'數量', '件數', '銷售數量', 'quantity', 'qty', 'count', 'quantity_sold'}
        amount_keywords = {'代收金額', '訂單金額', '總金額', '小計', 'total amount', 'total', '金額', 'total_amount', 'amount', 'revenue'}

        header_idx = None
        for idx, row in raw_df.iterrows():
            row_vals = [str(val).strip().lower() for val in row.values if pd.notna(val)]
            match_count = 0
            if any(kw in row_vals for kw in product_keywords):
                match_count += 1
            if any(kw in row_vals for kw in quantity_keywords):
                match_count += 1
            if any(kw in row_vals for kw in amount_keywords):
                match_count += 1
                
            if match_count >= 2:
                header_idx = idx
                break

        if header_idx is None:
            raise HTTPException(
                status_code=400,
                detail="無法辨識報表格式，請確認這是標準的賣貨便或 Shopify 訂單報表。"
            )

        if is_csv:
            try:
                df = pd.read_csv(io.BytesIO(contents), skiprows=header_idx, encoding=detected_encoding)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"無法解析 CSV 檔案：{str(e)}")
        else:
            try:
                df = pd.read_excel(io.BytesIO(contents), skiprows=header_idx, engine="openpyxl")
            except Exception:
                raise HTTPException(status_code=400, detail="無法解析 Excel 檔案，請確認檔案未損毀。")

        # Standardize column headers (strip whitespaces)
        df.columns = [str(col).strip() for col in df.columns]
        
        # X-Ray Diagnostic Logging
        print(f"Detected Columns: {df.columns.tolist()}")
        
        # Lists of accepted aliases (normalized to lowercase for English string comparisons)
        product_details_aliases = ['商品明細', '商品詳情', '訂購明細', '訂單內容', '商品資訊', 'product details', 'details', 'product_details']
        product_aliases = ['商品名稱', '品名', '商品', 'product name', 'sku', 'product_name', 'name']
        quantity_aliases = ['數量', '件數', '銷售數量', 'quantity', 'qty', 'count', 'quantity_sold']
        amount_aliases = ['代收金額', '訂單金額', '總金額', '小計', 'total amount', 'total', '金額', 'total_amount', 'amount', 'revenue']
        status_aliases = ['訂單狀態', '狀態', 'order status', 'status']
        
        rename_map = {}
        for col in df.columns:
            norm = col.lower()
            if norm in product_details_aliases:
                rename_map[col] = 'product_details'
            elif norm in product_aliases:
                if col not in rename_map:
                    rename_map[col] = 'product_name'
            elif norm in quantity_aliases:
                rename_map[col] = 'quantity'
            elif norm in amount_aliases:
                rename_map[col] = 'total_amount'
            elif norm in status_aliases:
                rename_map[col] = 'order_status'
                
        # Rename the matched columns
        df = df.rename(columns=rename_map)
        
        # 1. Status Filtering
        if 'order_status' in df.columns:
            invalid_statuses = {'取消', '退貨', 'cancelled', 'returned', 'refunded', 'cancel', 'return', 'fail', 'failed'}
            df = df[~df['order_status'].astype(str).str.strip().str.lower().isin(invalid_statuses)]
            
        # 2. Verify required columns exist (standard or combined)
        has_details = 'product_details' in df.columns
        has_name = 'product_name' in df.columns
        has_qty = 'quantity' in df.columns
        has_amount = 'total_amount' in df.columns
        
        is_valid = (has_name and has_qty and has_amount) or (has_details and has_amount)
        
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"檔案缺少必要欄位：請確保包含商品名稱、數量與金額欄位。系統實際讀取到的欄位有：{df.columns.tolist()}"
            )
            
        # Currency and Numeric Cleansing
        if 'total_amount' in df.columns:
            df['total_amount'] = df['total_amount'].astype(str).str.replace(r'[$,NT¥\s\u200b]', '', regex=True)
            df['total_amount'] = pd.to_numeric(df['total_amount'], errors='coerce')
            
        if 'quantity' in df.columns:
            df['quantity'] = df['quantity'].astype(str).str.replace(r'[$,NT¥\s\u200b]', '', regex=True)
            df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce')

        # 3. Product Details Parsing & Cell Splitting
        cleaned_rows = []
        if has_details:
            for idx, row in df.iterrows():
                details_val = str(row['product_details']).strip()
                if pd.isna(row['product_details']) or not details_val or details_val.lower() == 'nan':
                    continue
                    
                parts = re.split(r'[,\n\r;+，\uff0c\uff1b]', details_val)
                row_items = []
                for part in parts:
                    part_str = part.strip()
                    if not part_str:
                        continue
                        
                    m1 = re.match(r'^(.+?)\s*[*xX×\uff0a\u00d7]\s*(\d+)$', part_str)
                    m2 = re.match(r'^(\d+)\s*[*xX×\uff0a\u00d7]\s*(.+)$', part_str)
                    
                    if m1:
                        p_name = m1.group(1).strip()
                        p_qty = int(m1.group(2))
                    elif m2:
                        p_name = m2.group(2).strip()
                        p_qty = int(m2.group(1))
                    else:
                        p_name = part_str
                        p_qty = 1
                        
                    if p_name:
                        row_items.append((p_name, p_qty))
                        
                use_amount = row['total_amount'] if len(row_items) == 1 else None
                for p_name, p_qty in row_items:
                    cleaned_rows.append({
                        'product_name': p_name,
                        'quantity': p_qty,
                        'total_amount': use_amount
                    })
        else:
            df = df.dropna(subset=['product_name', 'quantity'])
            for idx, row in df.iterrows():
                p_name = str(row['product_name']).strip()
                if p_name:
                    cleaned_rows.append({
                        'product_name': p_name,
                        'quantity': row['quantity'],
                        'total_amount': row['total_amount']
                    })
                    
        # Final drop NaN or empty names
        rows_list = [r for r in cleaned_rows if r['product_name'] and r['product_name'].lower() != 'nan']
        
        if not rows_list:
            raise HTTPException(status_code=400, detail="檔案中沒有可匯入的有效數據列。")
            
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            for idx, row in enumerate(rows_list):
                prod_val = str(row['product_name']).strip()
                qty_raw = row['quantity']
                amount_raw = row['total_amount']
                
                if not prod_val:
                    raise HTTPException(status_code=400, detail=f"Row {idx+1}: Missing required cell values.")
                
                try:
                    qty_val = int(qty_raw)
                    if pd.isna(amount_raw) or amount_raw is None:
                        amount_val = None
                    else:
                        amount_val = float(amount_raw)
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail=f"Row {idx+1}: Quantity must be integer, Total Amount must be decimal.")
                
                if qty_val <= 0:
                    raise HTTPException(status_code=400, detail=f"Row {idx+1}: Quantity must be a positive integer.")
                # Find product matching by name OR SKU
                cursor.execute(
                    "SELECT sku, name, price FROM products WHERE user_id = ? AND (sku = ? OR name = ?);",
                    (user_id, prod_val, prod_val)
                )
                prod = cursor.fetchone()
                if not prod:
                    raise HTTPException(status_code=400, detail=f"Row {idx+1}: Product '{prod_val}' not found in catalog.")
                
                sku = prod["sku"]
                price = prod["price"]
                
                if amount_val is None:
                    amount_val = qty_val * price
                    
                if amount_val < 0:
                    raise HTTPException(status_code=400, detail=f"Row {idx+1}: Total Amount cannot be negative.")
                
                # Fetch inventory level
                cursor.execute(
                    "SELECT quantity FROM inventory_counts WHERE sku = ? AND user_id = ?;",
                    (sku, user_id)
                )
                count_row = cursor.fetchone()
                current_qty = count_row["quantity"] if count_row else 0
                
                if current_qty < qty_val:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Row {idx+1}: Insufficient stock for product '{prod_val}'. Current: {current_qty}, Requested: {qty_val}."
                    )
                
                # Deduct stock
                new_qty = current_qty - qty_val
                cursor.execute(
                    "UPDATE inventory_counts SET quantity = ? WHERE sku = ? AND user_id = ?;",
                    (new_qty, sku, user_id)
                )
                
                # Insert activity record
                cursor.execute(
                    """
                    INSERT INTO transactions (sku, user_id, type, quantity, price, revenue, transaction_date)
                    VALUES (?, ?, 'OUTBOUND', ?, ?, ?, ?);
                    """,
                    (sku, user_id, qty_val, price, amount_val, datetime.now().isoformat())
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except sqlite3.Error as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Database error during transaction: {str(e)}")
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=f"Failed to process file contents: {str(e)}")
        finally:
            conn.close()
            
        return {"success": True, "records_processed": len(rows_list), "errors": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {str(e)}")
