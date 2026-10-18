-- 0015 the app role reads clients and the extraction ledger (both company-wide metadata, no row filter needed)
GRANT SELECT ON clients, extractions TO nw_app;
