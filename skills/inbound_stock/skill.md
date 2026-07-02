---
name: inbound-stock-management
description: Procedural guidance for restocking inventory and registering new product SKUs in VibeInventory.
triggers:
  - inbound stock
  - restock item
  - register new product
  - register new SKU
---

# Inbound Stock Management Skill

This skill defines the procedural guidelines and business rules for recording inbound inventory shipments and cataloging new product SKUs in the VibeInventory system.

## 1. Context & Objectives
Inbound stock operations increase the physical stock level of a product. In VibeInventory, this requires modifying both the product definitions (`products` table) and the active counts (`inventory_counts` table), followed by appending a log to the `transactions` audit trail.

---

## 2. Inbound Workflow Procedures

### Step 1: SKU Extraction & Validation
*   Retrieve and sanitize the SKU input (trim whitespace and cast to uppercase).
*   Validate the incoming quantity:
    *   Must be an integer strictly greater than 0 (`quantity > 0`).
    *   Reject decimal values, negative numbers, or zero.

### Step 2: Database Catalog Search
*   Query the database to check if the SKU exists in the `products` table.
*   **Case A: SKU Exists**
    *   Retrieve the existing product's catalog details (Name, Price).
    *   Proceed to update the inventory level.
*   **Case B: SKU is New (Product Registration)**
    *   Verify that `name` and `price` fields are supplied in the request.
    *   If `name` or `price` is missing, reject the transaction with an HTTP 400 Bad Request error.
    *   Sanitize the product `name` (trim whitespace).
    *   Validate the unit `price` (must be a float $\ge 0.0$).
    *   Set the `low_stock_threshold` (default to `5` if not explicitly specified).
    *   Insert the new product record into the `products` table.
    *   Initialize the inventory count in `inventory_counts` to `0`.

### Step 3: Increment Inventory Levels
*   Update the `inventory_counts` table for the matching SKU:
    $$\text{New Quantity} = \text{Current Quantity} + \text{Inbound Quantity}$$

### Step 4: Record Transaction Log
*   Insert a transaction log row into the `transactions` table containing:
    *   `sku`: The target SKU ID.
    *   `type`: `'INBOUND'`.
    *   `quantity`: The quantity added.
    *   `price`: The unit price in the catalog (or registration price).
    *   `revenue`: Set strictly to `0.0` (inbound operations do not generate sales revenue).
    *   `transaction_date`: Current ISO 8601 timestamp.

---

## 3. Database Integrity & Constraints
*   **Atomic Transactions:** All database updates (registering a product, incrementing inventory count, and logging the transaction) must run inside a single database transaction block. If any step fails, roll back the entire transaction.
*   **Foreign Key Constraints:** The `sku` in `inventory_counts` and `transactions` must always reference a valid primary key in the `products` table.
