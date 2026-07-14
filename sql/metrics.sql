-- ============================================
-- RUNNING TOTALS: Orders
-- ============================================

-- Week-to-date (WTD) running total of orders
SELECT
    d.date,
    COUNT(*) AS orders,
    SUM(COUNT(*)) OVER (
        PARTITION BY d.iso_year, d.iso_week
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_orders_wtd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.iso_year, d.iso_week
ORDER BY d.date;

-- Month-to-date (MTD) running total of orders
SELECT
    d.date,
    COUNT(*) AS orders,
    SUM(COUNT(*)) OVER (
        PARTITION BY d.year, d.month
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_orders_mtd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.year, d.month
ORDER BY d.date;

-- Year-to-date (YTD) running total of orders
SELECT
    d.date,
    COUNT(*) AS orders,
    SUM(COUNT(*)) OVER (
        PARTITION BY d.year
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_orders_ytd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.year
ORDER BY d.date;

-- ============================================
-- RUNNING TOTALS: Funnel events (all stages, lead/signup/purchase)
-- ============================================

-- Week-to-date (WTD) running total, by stage
SELECT
    d.date,
    f.stage,
    COUNT(*) AS events,
    SUM(COUNT(*)) OVER (
        PARTITION BY f.stage, d.iso_year, d.iso_week
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_events_wtd
FROM fact_funnel_event f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, f.stage, d.iso_year, d.iso_week
ORDER BY f.stage, d.date;

-- Month-to-date (MTD) running total, by stage
SELECT
    d.date,
    f.stage,
    COUNT(*) AS events,
    SUM(COUNT(*)) OVER (
        PARTITION BY f.stage, d.year, d.month
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_events_mtd
FROM fact_funnel_event f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, f.stage, d.year, d.month
ORDER BY f.stage, d.date;

-- Year-to-date (YTD) running total, by stage
SELECT
    d.date,
    f.stage,
    COUNT(*) AS events,
    SUM(COUNT(*)) OVER (
        PARTITION BY f.stage, d.year
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_events_ytd
FROM fact_funnel_event f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, f.stage, d.year
ORDER BY f.stage, d.date;

-- ============================================
-- RUNNING TOTALS: Revenue
-- ============================================

-- Week-to-date (WTD) running total of revenue
SELECT
    d.date,
    SUM(f.revenue) AS revenue,
    SUM(SUM(f.revenue)) OVER (
        PARTITION BY d.iso_year, d.iso_week
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_revenue_wtd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.iso_year, d.iso_week
ORDER BY d.date;

-- Month-to-date (MTD) running total of revenue
SELECT
    d.date,
    SUM(f.revenue) AS revenue,
    SUM(SUM(f.revenue)) OVER (
        PARTITION BY d.year, d.month
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_revenue_mtd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.year, d.month
ORDER BY d.date;

-- Year-to-date (YTD) running total of revenue
SELECT
    d.date,
    SUM(f.revenue) AS revenue,
    SUM(SUM(f.revenue)) OVER (
        PARTITION BY d.year
        ORDER BY d.date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_revenue_ytd
FROM fact_orders f
JOIN dim_date d ON f.date_key = d.date_key
GROUP BY d.date, d.year
ORDER BY d.date;

-- ============================================
-- WoW: Orders
-- ============================================
WITH weekly_orders AS (
    SELECT d.iso_year, d.iso_week, COUNT(*) AS orders
    FROM fact_orders f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week
)
SELECT
    iso_year, iso_week, orders,
    LAG(orders) OVER (ORDER BY iso_year, iso_week) AS orders_prior_week,
    ROUND(
        100.0 * (orders - LAG(orders) OVER (ORDER BY iso_year, iso_week))
        / NULLIF(LAG(orders) OVER (ORDER BY iso_year, iso_week), 0),
    1) AS wow_pct
FROM weekly_orders
ORDER BY iso_year, iso_week;

-- ============================================
-- YoY: Orders
-- ============================================
WITH weekly_orders AS (
    SELECT d.iso_year, d.iso_week, COUNT(*) AS orders
    FROM fact_orders f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week
)
SELECT
    c.iso_week, c.iso_year, c.orders,
    p.orders AS orders_last_year,
    ROUND(100.0 * (c.orders - p.orders) / NULLIF(p.orders, 0), 1) AS yoy_pct
FROM weekly_orders c
LEFT JOIN weekly_orders p
       ON p.iso_year = c.iso_year - 1
      AND p.iso_week = c.iso_week
ORDER BY c.iso_week, c.iso_year;

-- ============================================
-- WoW: Funnel events, every stage
-- ============================================
WITH weekly_events AS (
    SELECT d.iso_year, d.iso_week, f.stage, COUNT(*) AS events
    FROM fact_funnel_event f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week, f.stage
)
SELECT
    stage, iso_year, iso_week, events,
    LAG(events) OVER (PARTITION BY stage ORDER BY iso_year, iso_week) AS events_prior_week,
    ROUND(
        100.0 * (events - LAG(events) OVER (PARTITION BY stage ORDER BY iso_year, iso_week))
        / NULLIF(LAG(events) OVER (PARTITION BY stage ORDER BY iso_year, iso_week), 0),
    1) AS wow_pct
FROM weekly_events
ORDER BY stage, iso_year, iso_week;

-- ============================================
-- YoY: Funnel events, every stage (generalized for any stage)
-- ============================================
WITH weekly_events AS (
    SELECT d.iso_year, d.iso_week, f.stage, COUNT(*) AS events
    FROM fact_funnel_event f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week, f.stage
)
SELECT
    c.stage, c.iso_week, c.iso_year, c.events,
    p.events AS events_last_year,
    ROUND(100.0 * (c.events - p.events) / NULLIF(p.events, 0), 1) AS yoy_pct
FROM weekly_events c
LEFT JOIN weekly_events p
       ON p.stage    = c.stage
      AND p.iso_year = c.iso_year - 1
      AND p.iso_week = c.iso_week
ORDER BY c.stage, c.iso_week, c.iso_year;

-- ============================================
-- WoW: Revenue
-- ============================================
WITH weekly_revenue AS (
    SELECT d.iso_year, d.iso_week, SUM(f.revenue) AS revenue
    FROM fact_orders f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week
)
SELECT
    iso_year, iso_week, revenue,
    LAG(revenue) OVER (ORDER BY iso_year, iso_week) AS revenue_prior_week,
    ROUND(
        (100.0 * (revenue - LAG(revenue) OVER (ORDER BY iso_year, iso_week))
        / NULLIF(LAG(revenue) OVER (ORDER BY iso_year, iso_week), 0))::numeric,
    1) AS wow_pct
FROM weekly_revenue
ORDER BY iso_year, iso_week;

-- ============================================
-- YoY: Revenue
-- ============================================
WITH weekly_revenue AS (
    SELECT d.iso_year, d.iso_week, SUM(f.revenue) AS revenue
    FROM fact_orders f
    JOIN dim_date d ON f.date_key = d.date_key
    GROUP BY d.iso_year, d.iso_week
)
SELECT
    c.iso_week, c.iso_year, c.revenue,
    p.revenue AS revenue_last_year,
    ROUND((100.0 * (c.revenue - p.revenue) / NULLIF(p.revenue, 0))::numeric, 1) AS yoy_pct
FROM weekly_revenue c
LEFT JOIN weekly_revenue p
       ON p.iso_year = c.iso_year - 1
      AND p.iso_week = c.iso_week
ORDER BY c.iso_week, c.iso_year;