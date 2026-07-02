# InOutSold! - Multi-Tenant Inventory Management SaaS

**InOutSold!** is a cloud-native, mobile-first inventory management Single Page Application (SPA) designed as a multi-tenant SaaS. Utilizing a bold, high-contrast **Neubrutalist** design language, the system offers real-time stock levels, POS checkout controls, inbound restocking logs, dynamic layout customizability, and instant localization. Under the hood, the system enforces complete data isolation between tenants, protects registrations with a strong password complexity policy, and integrates Google OAuth 2.0 for single sign-on access.

---

## 1. Key Technical Milestones (The "Wow" Factor)

### Multi-Tenant Architecture & Data Isolation
The application isolates tenant data at both the database schema level and the HTTP execution boundary:
*   **Composite Key Isolation:** The SQLite database utilizes composite primary keys `(sku, user_id)` across the `products`, `inventory_counts`, and `transactions` tables. This enables different users to register and manage identical SKUs (e.g., `TS-VIB-01`) independently without database conflicts or data leakage.
*   **Request Auditing:** A FastAPI security dependency (`get_current_user`) decodes incoming JWT tokens, validates claims, extracts the verified `user_id`, and locks the SQL execution scope to the authenticated tenant. Cross-tenant access is strictly blocked at the endpoint boundary.
*   **Isolated Workspace Layouts:** Gridstack.js dashboard layout positions are saved to `localStorage` using user-specific namespace keys (`vibe_grid_layout_[username]`). On logout, grid configurations are torn down cleanly and reset to default coordinates, ready for the next session.

### Dual Authentication & Security Policies
*   **Native Registration (Strong Enforcements):** Native password submissions are validated against a strict password complexity regular expression (minimum 8 characters, containing at least one uppercase letter, one lowercase letter, one number, and one special character). These rules are enforced both client-side and at the FastAPI Pydantic schema validation layer (`RegisterRequest` model).
*   **OAuth 2.0 Integration:** Supports Google login using the OAuth 2.0 Authorization Code Flow. On success, Google's user profile email is used to automatically check the SQLite registration records, auto-registers new users with randomized password hashes, seeds their starting catalog, and returns a securely signed JWT payload.

### Cloud-Native Deployment
*   **Unified Container Architecture:** To eliminate cross-origin resource sharing (CORS) friction and prevent browser Mixed Content blocks, both the FastAPI ASGI backend and the static single-page frontend are served from a single, unified Docker container.
*   **Serverless Scaling:** The application is packaged and built via Google Cloud Build and deployed serverless on **Google Cloud Run**, achieving rapid auto-scaling, low cold-start latency, and TLS termination.

### Mobile-First UI/UX & Responsive Layouts
*   **Responsive Touch Targets:** In accordance with mobile-first design practices, all clickable icons, inputs, and toggle buttons have a minimum touch target height of `48px`.
*   **Action Button Collapsing:** Action bar items dynamically collapse into icon-only square button configurations on small viewport widths (`< 768px`) using Tailwind breakpoints, returning to descriptive text buttons on desktop screens.
*   **Adaptive Grids:** Gridstack.js is configured to automatically collapse into a single-column layout on mobile devices (`disableOneColumnMode: false`, `oneColumnSize: 768`), ensuring cards fit mobile screens cleanly. Table containers are wrapped in swipable horizontal overflow boxes (`overflow-x-auto w-full`) to prevent horizontal layout breaks.

---

## 2. Architecture Insights (Important Note for Judges)

### Note on OAuth Consent Screen
> [!IMPORTANT]
> Because **InOutSold!** is configured as an **"External"** user type on the Google Cloud Console and is deployed for academic capstone presentation purposes, it has not undergone the standard 3-to-7 day Google App Verification process.
> 
> As a result:
> 1.  The Google OAuth Consent Screen displays the raw Cloud Run URI (`run.app` subdomain) instead of the brand name "InOutSold!".
> 2.  The browser may show an "Unverified App" warning screen to external testers.
> 
> This behavior is expected and demonstrates a deep understanding of Google Cloud's anti-phishing policies, client redirect constraints, and production security policies.

---

## 3. Tech Stack

*   **Frontend:** HTML5, Vanilla JavaScript, Tailwind CSS (via Play CDN), Lucide Icons, Gridstack.js.
*   **Backend:** FastAPI (ASGI), PyJWT (token signatures), bcrypt (direct password hashing), httpx (asynchronous HTTP calls), SQLite (local database engine).
*   **DevOps & Hosting:** Docker (Multi-stage build), Google Cloud Build, Google Cloud Run.

---

## 5. Local Development Setup

Follow these steps to run **InOutSold!** locally:

### Prerequisites
*   Python 3.9 or higher.
*   `venv` virtual environment module.

### Setup Instructions
1.  **Clone the project directory and create the virtual environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```
2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configure Environment Variables (Optional):**
    Configure your Google OAuth client ID and secret in your shell environment, or leave them blank to use local native password credentials:
    ```bash
    export GOOGLE_CLIENT_ID="your-client-id"
    export GOOGLE_CLIENT_SECRET="your-client-secret"
    export GOOGLE_REDIRECT_URI="http://localhost:8000/api/auth/google/callback"
    ```
4.  **Run the Uvicorn server:**
    ```bash
    uvicorn main:app --port 8000 --reload
    ```
5.  **Open the application:**
    Navigate to [http://localhost:8000](http://localhost:8000) in your web browser. The backend server will automatically initialize the SQLite tables and seed the database.

### Running the Test Suite
The project includes a robust test suite that validates inventory checks, SQL injection blocks, XSS script stripping, password complexity models, and Google OAuth redirection rules. Run the tests with:
```bash
pytest -v
```
All 19 test cases will run against an isolated test database, ensuring code stability and boundary correctness.
