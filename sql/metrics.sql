-- 1. Running total of orders within each month (resets every month)
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

-- 2. Week-over-week leads (this ISO week vs the immediately preceding one)
WITH weekly_leads AS (
    SELECT d.year, d.iso_week, COUNT(*) AS leads
    FROM fact_funnel_event f
    JOIN dim_date d ON f.date_key = d.date_key
    WHERE f.stage = 'lead'
    GROUP BY d.year, d.iso_week
)
SELECT
    year, iso_week, leads,
    LAG(leads) OVER (ORDER BY year, iso_week) AS leads_prior_week,
    ROUND(
        100.0 * (leads - LAG(leads) OVER (ORDER BY year, iso_week))
        / NULLIF(LAG(leads) OVER (ORDER BY year, iso_week), 0),
    1) AS wow_pct
FROM weekly_leads
ORDER BY year, iso_week;

-- 3. Year-over-year leads, matched by the same ISO week number
WITH weekly_leads AS (
    SELECT d.year, d.iso_week, COUNT(*) AS leads
    FROM fact_funnel_event f
    JOIN dim_date d ON f.date_key = d.date_key
    WHERE f.stage = 'lead'
    GROUP BY d.year, d.iso_week
)
SELECT
    iso_week, year, leads,
    LAG(leads) OVER (PARTITION BY iso_week ORDER BY year) AS leads_last_year,
    ROUND(
        100.0 * (leads - LAG(leads) OVER (PARTITION BY iso_week ORDER BY year))
        / NULLIF(LAG(leads) OVER (PARTITION BY iso_week ORDER BY year), 0),
    1) AS yoy_pct
FROM weekly_leads
ORDER BY iso_week, year;