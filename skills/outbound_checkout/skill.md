---
name: outbound-checkout-pos
description: Strict business rules for recording sales, checking stock levels, and processing outbound transactions in VibeInventory.
triggers:
  - outbound stock
  - sell item
  - process checkout
  - POS sale
---

# Outbound POS Checkout Skill

This skill defines the business logic rules and validation constraints for processing product sales (outbound operations) and calculating revenue in the VibeInventory system.

## 1. Context & Objectives
Outbound stock transactions decrease the physical stock level of a product. In VibeInventory, this requires checking available stock, updating the counts (`inventory_counts` table), calculating transaction revenue, and logging the event in the `transactions` audit trail.

---

## 2. Validation & Business Rules

### Rule 1: Product SKU Verification
*   Verify that the requested SKU exists in the database.
*   If the SKU is not found, reject the transaction with an HTTP 404 Not Found error.

### Rule 2: Stock Availability Check (Critical)
*   Query `inventory_counts` to check the current physical quantity in stock for the SKU.
*   Compare available quantity with requested checkout quantity:
    *   If $\text{Current Quantity} < \text{Requested Quantity}$, reject the checkout immediately.
    *   Throw a clear HTTP 400 Bad Request error indicating the shortage:
        `"Insufficient stock for [Product Name] (Only [Current Quantity] remaining)"`
    *   Do **NOT** allow negative stock levels under any circumstances.

### Rule 3: Quantity & Price Validation
*   The checkout quantity must be an integer strictly greater than 0 (`quantity > 0`).
*   The unit price must be a float $\ge 0.0$.
    *   If a custom price is provided, use it.
    *   If no price is provided, default to the catalog price defined in the `products` table.

---

## 3. Checkout Processing Workflow

### Step 1: Deduct Inventory Levels
*   Update the `inventory_counts` table for the matching SKU:
    $$\text{New Quantity} = \text{Current Quantity} - \text{Checkout Quantity}$$

### Step 2: Calculate Transaction Revenue
*   Compute total revenue for the transaction:
    $$\text{Revenue} = \text{Checkout Quantity} \times \text{Unit Price}$$

### Step 3: Insert Transaction Audit Log
*   Insert a transaction log row into the `transactions` table containing:
    *   `sku`: The target SKU ID.
    *   `type`: `'OUTBOUND'`.
    *   `quantity`: The checkout quantity.
    *   `price`: The unit price applied.
    *   `revenue`: The computed total revenue.
    *   `transaction_date`: Current ISO 8601 timestamp.

---

## 4. Transaction Integrity
*   **Isolation & Atomic Commit:** All database operations (fetching quantity, decrementing count, and writing transaction logs) must run within a single isolated database transaction. If any operation fails, the transaction must roll back to avoid mismatched counts and audit tables.
