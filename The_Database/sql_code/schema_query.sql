SELECT
    name AS table_name,
    sql AS table_definition
FROM main.sqlite_schema
WHERE type = 'table'
  AND name NOT LIKE 'sqlite_%'
ORDER BY name;

-- EXECUTE COMMANT:
-- sqlite3 The_Database/job_data.db < The_Database/sql_code/schema_query.sql