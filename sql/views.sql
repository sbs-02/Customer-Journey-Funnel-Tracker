-- ============================================================================
-- Governed semantic layer.
--
-- One definition of "a week", "a lead", "revenue" -- shared by Power BI, the
-- MCP agent, and sql/metrics.sql.
--
-- THE WEEK KEY IS (iso_year, iso_week) -- NEVER (year, iso_week).
-- Calendar year and ISO year disagree at year boundaries: 2022-01-01 is ISO
-- week 52 of ISO year 2021. Keying weekly metrics on the calendar year merges
-- January into December -- a 364-day "week".
-- ============================================================================

-- Week spine, with a completeness flag.
-- Weeks at the edges of the loaded range are PARTIAL: ISO year 2021 week 52
-- holds only 2022-01-01..02, because December 2021 is outside our data range.
-- Comparing a 2-day week against a 7-day week year-over-year is not a real
-- comparison, so completeness is exposed rather than silently ignored.
CREATE OR REPLACE VIEW vw_week AS
SELECT
    iso_year,
    iso_week,
    MIN(week_start_date) AS week_start_date,
    MIN(date)            AS first_date_observed,
    MAX(date)            AS last_date_observed,
    COUNT(*)             AS days_observed,
    (COUNT(*) = 7)       AS is_complete_week
FROM dim_date
GROUP BY iso_year, iso_week;

CREATE OR REPLACE VIEW vw_weekly_funnel AS
SELECT
    d.iso_year, d.iso_week, w.week_start_date, w.is_complete_week,
    f.stage, COUNT(*) AS events
FROM fact_funnel_event f
JOIN dim_date d ON f.date_key = d.date_key
JOIN vw_week  w ON w.iso_year = d.iso_year AND w.iso_week = d.iso_week
GROUP BY d.iso_year, d.iso_week, w.week_start_date, w.is_complete_week, f.stage;

CREATE OR REPLACE VIEW vw_weekly_orders AS
SELECT
    d.iso_year, d.iso_week, w.week_start_date, w.is_complete_week,
    COUNT(*)       AS orders,
    SUM(o.revenue) AS revenue,
    SUM(o.quantity) AS units
FROM fact_orders o
JOIN dim_date d ON o.date_key = d.date_key
JOIN vw_week  w ON w.iso_year = d.iso_year AND w.iso_week = d.iso_week
GROUP BY d.iso_year, d.iso_week, w.week_start_date, w.is_complete_week;

-- The funnel, pivoted: one row per week, each stage a column, plus conversion
-- and drop-off. This is the shape the Power BI funnel page and the agent's
-- funnel tool both want.
--
-- NULLIF guards every denominator: a week with zero visits yields NULL
-- (unknown), not a divide-by-zero and not a misleading 0%.
CREATE OR REPLACE VIEW vw_funnel_weekly AS
WITH staged AS (
    SELECT
        iso_year, iso_week, week_start_date, is_complete_week,
        SUM(CASE WHEN stage = 'visit'       THEN events ELSE 0 END) AS visits,
        SUM(CASE WHEN stage = 'lead'        THEN events ELSE 0 END) AS leads,
        SUM(CASE WHEN stage = 'opportunity' THEN events ELSE 0 END) AS opportunities
    FROM vw_weekly_funnel
    GROUP BY iso_year, iso_week, week_start_date, is_complete_week
)
SELECT
    s.iso_year, s.iso_week, s.week_start_date, s.is_complete_week,
    s.visits, s.leads, s.opportunities,
    COALESCE(o.orders, 0)  AS orders,
    COALESCE(o.revenue, 0) AS revenue,
    ROUND(100.0 * s.leads         / NULLIF(s.visits, 0), 2)        AS visit_to_lead_pct,
    ROUND(100.0 * s.opportunities / NULLIF(s.leads,  0), 2)        AS lead_to_opp_pct,
    ROUND(100.0 * COALESCE(o.orders,0) / NULLIF(s.opportunities,0), 2) AS opp_to_order_pct,
    ROUND(100.0 * COALESCE(o.orders,0) / NULLIF(s.visits, 0), 2)   AS visit_to_order_pct
FROM staged s
LEFT JOIN vw_weekly_orders o
       ON o.iso_year = s.iso_year AND o.iso_week = s.iso_week;

-- WoW and YoY per funnel stage.
--
-- WoW uses LAG over the week sequence -- adjacent weeks, so LAG is exact.
--
-- YoY uses an explicit LEFT JOIN on iso_year - 1, NOT
-- LAG(...) OVER (PARTITION BY iso_week ORDER BY iso_year).
-- LAG returns the previous row *present in the partition*, which is not
-- necessarily the prior year: if a week is missing for one year, LAG silently
-- compares 2026 against 2024 and labels it "year over year". The join compares
-- the prior year or returns NULL -- it cannot quietly compare the wrong pair.
CREATE OR REPLACE VIEW vw_funnel_stage_trend AS
SELECT
    c.stage, c.iso_year, c.iso_week, c.week_start_date, c.is_complete_week, c.events,
    LAG(c.events) OVER (PARTITION BY c.stage ORDER BY c.iso_year, c.iso_week)
        AS events_prior_week,
    ROUND(100.0 * (c.events - LAG(c.events) OVER (PARTITION BY c.stage ORDER BY c.iso_year, c.iso_week))
        / NULLIF(LAG(c.events) OVER (PARTITION BY c.stage ORDER BY c.iso_year, c.iso_week), 0), 1)
        AS wow_pct,
    p.events AS events_last_year,
    ROUND(100.0 * (c.events - p.events) / NULLIF(p.events, 0), 1) AS yoy_pct
FROM vw_weekly_funnel c
LEFT JOIN vw_weekly_funnel p
       ON p.stage    = c.stage
      AND p.iso_year = c.iso_year - 1
      AND p.iso_week = c.iso_week;

CREATE OR REPLACE VIEW vw_orders_trend AS
SELECT
    c.iso_year, c.iso_week, c.week_start_date, c.is_complete_week,
    c.orders, c.revenue,
    LAG(c.orders)  OVER (ORDER BY c.iso_year, c.iso_week) AS orders_prior_week,
    LAG(c.revenue) OVER (ORDER BY c.iso_year, c.iso_week) AS revenue_prior_week,
    ROUND(100.0 * (c.orders - LAG(c.orders) OVER (ORDER BY c.iso_year, c.iso_week))
        / NULLIF(LAG(c.orders) OVER (ORDER BY c.iso_year, c.iso_week), 0), 1)
        AS orders_wow_pct,
    ROUND((100.0 * (c.revenue - LAG(c.revenue) OVER (ORDER BY c.iso_year, c.iso_week))
        / NULLIF(LAG(c.revenue) OVER (ORDER BY c.iso_year, c.iso_week), 0))::numeric, 1)
        AS revenue_wow_pct,
    p.orders  AS orders_last_year,
    p.revenue AS revenue_last_year,
    ROUND(100.0 * (c.orders - p.orders) / NULLIF(p.orders, 0), 1) AS orders_yoy_pct,
    ROUND((100.0 * (c.revenue - p.revenue) / NULLIF(p.revenue, 0))::numeric, 1)
        AS revenue_yoy_pct
FROM vw_weekly_orders c
LEFT JOIN vw_weekly_orders p
       ON p.iso_year = c.iso_year - 1
      AND p.iso_week = c.iso_week;