-- DBA reference only. The analyzer never runs this DDL or modifies source rows.
-- This example stores Korea wall-clock time; configure timestamp_timezone="+09:00".
CREATE TABLE APP_ERROR_LOG (
    EVENT_ID NUMBER(38, 0) PRIMARY KEY,
    OCCURRED_AT TIMESTAMP(6) NOT NULL,
    SERVICE_NAME VARCHAR2(200 CHAR) NOT NULL,
    LOG_LEVEL VARCHAR2(20 CHAR) NOT NULL,
    ERROR_MESSAGE CLOB NOT NULL,
    STACK_TRACE CLOB
);

CREATE INDEX IX_APP_ERROR_LOG_TIME ON APP_ERROR_LOG (OCCURRED_AT, EVENT_ID);

-- Grant SELECT on the actual owner/table to an existing runtime account:
-- GRANT SELECT ON LOG_OWNER.APP_ERROR_LOG TO LOG_ANALYZER;
-- Cross-schema config: table="LOG_OWNER.APP_ERROR_LOG"
-- Use a view if joins, custom predicates or legacy timestamp conversions are needed.
