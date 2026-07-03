import sqlite3
import os

def migrate():
    db_files = ["vibe_inventory.db", "vibe_inventory_test.db"]
    for db_file in db_files:
        if not os.path.exists(db_file):
            continue
        
        print(f"Checking database: {db_file}")
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        try:
            # Check the CREATE SQL of the transactions table
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions';")
            row = cursor.fetchone()
            if not row:
                print("Transactions table does not exist. Skipping.")
                conn.close()
                continue
                
            table_sql = row[0]
            # Check if there is any UNIQUE constraint on user_id
            if "UNIQUE" in table_sql.upper() and "USER_ID" in table_sql.upper():
                print("Found UNIQUE constraint on user_id. Rebuilding table...")
                
                # Begin transaction
                cursor.execute("BEGIN TRANSACTION;")
                
                # Rename old table
                cursor.execute("ALTER TABLE transactions RENAME TO transactions_old;")
                
                # Recreate table without UNIQUE constraint
                cursor.execute("""
                CREATE TABLE transactions (
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
                
                # Copy data
                cursor.execute("""
                INSERT INTO transactions (id, sku, user_id, type, quantity, price, revenue, transaction_date)
                SELECT id, sku, user_id, type, quantity, price, revenue, transaction_date FROM transactions_old;
                """)
                
                # Drop old table
                cursor.execute("DROP TABLE transactions_old;")
                
                # Commit
                conn.commit()
                print("Migration completed successfully.")
            else:
                print("No UNIQUE constraint on user_id detected in transactions table.")
                
        except Exception as e:
            conn.rollback()
            print(f"Error during migration of {db_file}: {e}")
        finally:
            conn.close()

if __name__ == "__main__":
    migrate()
