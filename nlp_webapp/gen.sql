-- ===========================================================
-- DEMO RETAIL într-un schema separat ca să nu calce peste public
-- ===========================================================
CREATE SCHEMA IF NOT EXISTS demo_retail;
SET search_path = demo_retail, public;

-- ========== LOCALIZARE ==========
CREATE TABLE IF NOT EXISTS countries (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS cities (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  country_id  BIGINT NOT NULL REFERENCES countries(id) ON DELETE RESTRICT,
  name        TEXT NOT NULL,
  UNIQUE(country_id, name)
);

CREATE TABLE IF NOT EXISTS addresses (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  city_id     BIGINT NOT NULL REFERENCES cities(id) ON DELETE RESTRICT,
  street      TEXT NOT NULL,
  postal_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_addresses_city ON addresses(city_id);

-- ========== ENTITĂȚI COMERCIALE ==========
CREATE TABLE IF NOT EXISTS stores (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL,
  address_id  BIGINT REFERENCES addresses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_stores_address ON stores(address_id);

CREATE TABLE IF NOT EXISTS warehouses (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL,
  address_id  BIGINT REFERENCES addresses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_wh_address ON warehouses(address_id);

CREATE TABLE IF NOT EXISTS suppliers (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL,
  address_id  BIGINT REFERENCES addresses(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_suppliers_address ON suppliers(address_id);

-- ========== CLIENTS ==========
CREATE TABLE IF NOT EXISTS customers_demo (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  full_name   TEXT NOT NULL,
  email       TEXT UNIQUE,
  phone       TEXT,
  address_id  BIGINT REFERENCES addresses(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_customers_address ON customers_demo(address_id);

-- ========== HR ==========
CREATE TABLE IF NOT EXISTS departments (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  store_id    BIGINT REFERENCES stores(id) ON DELETE SET NULL,
  name        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_departments_store ON departments(store_id);

CREATE TABLE IF NOT EXISTS employees (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id BIGINT REFERENCES departments(id) ON DELETE SET NULL,
  manager_id    BIGINT REFERENCES employees(id) ON DELETE SET NULL,
  full_name     TEXT NOT NULL,
  role          TEXT,
  hired_at      DATE DEFAULT CURRENT_DATE
);
CREATE INDEX IF NOT EXISTS idx_employees_dept ON employees(department_id);
CREATE INDEX IF NOT EXISTS idx_employees_manager ON employees(manager_id);

-- ========== PRODUSE & CATEGORII ==========
CREATE TABLE IF NOT EXISTS categories (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  parent_id   BIGINT REFERENCES categories(id) ON DELETE SET NULL,
  name        TEXT NOT NULL,
  UNIQUE(parent_id, name)
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);

CREATE TABLE IF NOT EXISTS products_demo (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sku           TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  category_id   BIGINT REFERENCES categories(id) ON DELETE SET NULL,
  price         NUMERIC(12,2) NOT NULL,
  active        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products_demo(category_id);

-- many-to-many extra (produs în mai multe categorii)
CREATE TABLE IF NOT EXISTS product_categories (
  product_id   BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  category_id  BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  PRIMARY KEY(product_id, category_id)
);

-- ========== INVENTAR ==========
CREATE TABLE IF NOT EXISTS inventory (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  warehouse_id  BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
  product_id    BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  quantity      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(warehouse_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_prod ON inventory(product_id);

CREATE TABLE IF NOT EXISTS stock_movements (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_id         BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  from_warehouse_id  BIGINT REFERENCES warehouses(id) ON DELETE SET NULL,
  to_warehouse_id    BIGINT REFERENCES warehouses(id) ON DELETE SET NULL,
  quantity           INTEGER NOT NULL,
  reason             TEXT,
  created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mov_prod ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_mov_from ON stock_movements(from_warehouse_id);
CREATE INDEX IF NOT EXISTS idx_mov_to   ON stock_movements(to_warehouse_id);

-- ========== APROVIZIONARE ==========
CREATE TABLE IF NOT EXISTS supplier_products (
  supplier_id  BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
  product_id   BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  supplier_sku TEXT,
  cost         NUMERIC(12,2),
  PRIMARY KEY (supplier_id, product_id)
);

CREATE TABLE IF NOT EXISTS purchases (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  supplier_id    BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
  warehouse_id   BIGINT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  ordered_at     TIMESTAMPTZ DEFAULT now(),
  received_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_purchases_sup ON purchases(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchases_wh  ON purchases(warehouse_id);

CREATE TABLE IF NOT EXISTS purchase_items (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purchase_id   BIGINT NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
  product_id    BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE RESTRICT,
  qty           INTEGER NOT NULL,
  cost          NUMERIC(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pitems_purchase ON purchase_items(purchase_id);
CREATE INDEX IF NOT EXISTS idx_pitems_product  ON purchase_items(product_id);

-- ========== VÂNZARE (COMENZI) ==========
CREATE TABLE IF NOT EXISTS orders_demo (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id    BIGINT REFERENCES customers_demo(id) ON DELETE SET NULL,
  store_id       BIGINT REFERENCES stores(id) ON DELETE SET NULL,
  shipping_addr  BIGINT REFERENCES addresses(id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'new', -- new/paid/packed/shipped/delivered/canceled
  created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders_demo(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_store    ON orders_demo(store_id);

CREATE TABLE IF NOT EXISTS order_items_demo (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  product_id  BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE RESTRICT,
  qty         INTEGER NOT NULL CHECK (qty > 0),
  unit_price  NUMERIC(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oitems_order   ON order_items_demo(order_id);
CREATE INDEX IF NOT EXISTS idx_oitems_product ON order_items_demo(product_id);

CREATE TABLE IF NOT EXISTS payments (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  method      TEXT NOT NULL, -- card/cod/transfer/etc.
  amount      NUMERIC(12,2) NOT NULL,
  paid_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);

-- ========== LIVRĂRI ==========
CREATE TABLE IF NOT EXISTS shipments (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id      BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  warehouse_id  BIGINT REFERENCES warehouses(id) ON DELETE SET NULL,
  carrier       TEXT,
  tracking_no   TEXT,
  shipped_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);

CREATE TABLE IF NOT EXISTS shipment_items (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  shipment_id   BIGINT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
  order_item_id BIGINT NOT NULL REFERENCES order_items_demo(id) ON DELETE RESTRICT,
  qty           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sitems_shipment ON shipment_items(shipment_id);
CREATE INDEX IF NOT EXISTS idx_sitems_oitm     ON shipment_items(order_item_id);

-- ========== FACTURARE ==========
CREATE TABLE IF NOT EXISTS invoices (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  issued_at   TIMESTAMPTZ DEFAULT now(),
  total       NUMERIC(12,2) NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_order ON invoices(order_id);

CREATE TABLE IF NOT EXISTS invoice_items (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id    BIGINT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  order_item_id BIGINT NOT NULL REFERENCES order_items_demo(id) ON DELETE RESTRICT,
  line_total    NUMERIC(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iitems_invoice ON invoice_items(invoice_id);

-- ========== RETURURI ==========
CREATE TABLE IF NOT EXISTS returns (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  reason      TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_returns_order ON returns(order_id);

CREATE TABLE IF NOT EXISTS return_items (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  return_id     BIGINT NOT NULL REFERENCES returns(id) ON DELETE CASCADE,
  order_item_id BIGINT NOT NULL REFERENCES order_items_demo(id) ON DELETE RESTRICT,
  qty           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ritems_return ON return_items(return_id);

-- ========== MARKETING ==========
CREATE TABLE IF NOT EXISTS promotions (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL,
  starts_at   DATE,
  ends_at     DATE
);

CREATE TABLE IF NOT EXISTS product_promotions (
  product_id    BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  promotion_id  BIGINT NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
  discount_pct  NUMERIC(5,2) CHECK (discount_pct BETWEEN 0 AND 100),
  PRIMARY KEY (product_id, promotion_id)
);

CREATE TABLE IF NOT EXISTS coupons (
  id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code      TEXT NOT NULL UNIQUE,
  discount_pct NUMERIC(5,2) CHECK (discount_pct BETWEEN 0 AND 100),
  valid_until DATE
);

CREATE TABLE IF NOT EXISTS coupon_redemptions (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  coupon_id   BIGINT NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
  order_id    BIGINT NOT NULL REFERENCES orders_demo(id) ON DELETE CASCADE,
  customer_id BIGINT REFERENCES customers_demo(id) ON DELETE SET NULL,
  redeemed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_credeem_coupon ON coupon_redemptions(coupon_id);
CREATE INDEX IF NOT EXISTS idx_credeem_order  ON coupon_redemptions(order_id);

-- ========== FEEDBACK & LOIALITATE ==========
CREATE TABLE IF NOT EXISTS reviews (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers_demo(id) ON DELETE CASCADE,
  product_id  BIGINT NOT NULL REFERENCES products_demo(id) ON DELETE CASCADE,
  rating      INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment     TEXT,
  created_at  TIMESTAMPTZ DEFAULT now(),
  UNIQUE(customer_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews(product_id);

CREATE TABLE IF NOT EXISTS loyalty_accounts (
  customer_id BIGINT PRIMARY KEY REFERENCES customers_demo(id) ON DELETE CASCADE,
  points      BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS loyalty_transactions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id    BIGINT NOT NULL REFERENCES loyalty_accounts(customer_id) ON DELETE CASCADE,
  order_id      BIGINT REFERENCES orders_demo(id) ON DELETE SET NULL,
  points_delta  INT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_loytx_account ON loyalty_transactions(account_id);

-- ===========================================================
-- SEED MINIM (arce vizibile în diagramă)
-- ===========================================================
INSERT INTO countries (name) VALUES ('România') ON CONFLICT DO NOTHING;
INSERT INTO cities (country_id, name)
SELECT c.id, 'București' FROM countries c WHERE c.name='România'
ON CONFLICT DO NOTHING;

INSERT INTO addresses (city_id, street, postal_code)
SELECT ci.id, 'Bd. Unirii 1', '010101' FROM cities ci WHERE ci.name='București'
ON CONFLICT DO NOTHING;

INSERT INTO stores (name, address_id)
SELECT 'Magazin Centrul Vechi', a.id FROM addresses a LIMIT 1
ON CONFLICT DO NOTHING;

INSERT INTO warehouses (name, address_id)
SELECT 'Depozit Nord', a.id FROM addresses a LIMIT 1
ON CONFLICT DO NOTHING;

INSERT INTO suppliers (name, address_id)
SELECT 'TechParts SRL', a.id FROM addresses a LIMIT 1
ON CONFLICT DO NOTHING;

INSERT INTO categories (name) VALUES ('Electronice') ON CONFLICT DO NOTHING;
INSERT INTO categories (name, parent_id)
SELECT 'Laptopuri', c.id FROM categories c WHERE c.name='Electronice'
ON CONFLICT DO NOTHING;

INSERT INTO products_demo (sku, name, category_id, price) VALUES
('SKU-LAP-001','Laptop Ultrabook 13"', (SELECT id FROM categories WHERE name='Laptopuri' LIMIT 1), 4999.99),
('SKU-LAP-002','Laptop Gaming 15"',  (SELECT id FROM categories WHERE name='Laptopuri' LIMIT 1), 6999.90)
ON CONFLICT DO NOTHING;

INSERT INTO customers_demo (full_name, email, address_id)
SELECT 'Ion Popescu','ion@example.com', a.id FROM addresses a LIMIT 1
ON CONFLICT DO NOTHING;

INSERT INTO inventory (warehouse_id, product_id, quantity)
SELECT w.id, p.id, 50 FROM warehouses w CROSS JOIN LATERAL (SELECT id FROM products_demo ORDER BY id LIMIT 1) p
ON CONFLICT (warehouse_id, product_id) DO NOTHING;

INSERT INTO inventory (warehouse_id, product_id, quantity)
SELECT w.id, p.id, 30 FROM warehouses w CROSS JOIN LATERAL (SELECT id FROM products_demo ORDER BY id DESC LIMIT 1) p
ON CONFLICT (warehouse_id, product_id) DO NOTHING;

INSERT INTO orders_demo (customer_id, store_id, shipping_addr, status)
SELECT c.id, s.id, a.id, 'paid'
FROM customers_demo c, stores s, addresses a
LIMIT 1;

INSERT INTO order_items_demo (order_id, product_id, qty, unit_price)
SELECT o.id, p.id, 1, p.price
FROM orders_demo o, products_demo p
WHERE NOT EXISTS (SELECT 1 FROM order_items_demo oi WHERE oi.order_id=o.id AND oi.product_id=p.id)
LIMIT 2;

INSERT INTO payments (order_id, method, amount)
SELECT o.id, 'card', (SELECT SUM(qty*unit_price) FROM order_items_demo WHERE order_id=o.id)
FROM orders_demo o
WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id=o.id);

INSERT INTO invoices (order_id, issued_at, total)
SELECT o.id, now(), (SELECT SUM(qty*unit_price) FROM order_items_demo WHERE order_id=o.id)
FROM orders_demo o
ON CONFLICT DO NOTHING;

INSERT INTO shipments (order_id, warehouse_id, carrier, tracking_no, shipped_at)
SELECT o.id, w.id, 'CourierX', 'TRK123', now()
FROM orders_demo o, warehouses w
ON CONFLICT DO NOTHING;

INSERT INTO shipment_items (shipment_id, order_item_id, qty)
SELECT s.id, oi.id, oi.qty
FROM shipments s
JOIN orders_demo o ON o.id = s.order_id
JOIN order_items_demo oi ON oi.order_id = o.id
ON CONFLICT DO NOTHING;

INSERT INTO reviews (customer_id, product_id, rating, comment)
SELECT c.id, p.id, 5, 'Excelent!'
FROM customers_demo c, products_demo p
WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.customer_id=c.id AND r.product_id=p.id)
LIMIT 1;

INSERT INTO loyalty_accounts (customer_id)
SELECT c.id FROM customers_demo c
ON CONFLICT DO NOTHING;

INSERT INTO loyalty_transactions (account_id, order_id, points_delta)
SELECT c.id, o.id, 50
FROM customers_demo c
JOIN orders_demo o ON o.customer_id=c.id
ON CONFLICT DO NOTHING;
