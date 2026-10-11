-- Migration 002: add clinic_id to panchakarma_resources.
--
-- Decision (Phase A, migration-infrastructure milestone): panchakarma_resources
-- is currently unused by any application code -- grepping the whole
-- codebase for "panchakarma" turns up nothing outside its own CREATE TABLE
-- statement. But by name and shape (name, quantity, resource_type) it
-- represents a clinic's own physical resources, exactly the same kind of
-- thing `inventory` already is -- it just hasn't been wired to an endpoint
-- yet. Multi-clinic isolation (Phase A, step 3) will need every
-- clinic-owned table scoped by clinic_id, and retrofitting this one later,
-- after RBAC/isolation code already exists and has to special-case an
-- inconsistent table, is strictly more error-prone than doing it now while
-- the table is still empty and inert. So: treated as clinic-owned, given
-- the same clinic_id column and default every other clinic-owned table
-- already has.
--
-- No IF NOT EXISTS here, unlike migration 001 -- unlike that one, this
-- migration was never a pre-existing fact about the live database, so
-- there is no "already exists from before migrations existed" case to be
-- safe against. The schema_migrations tracking is what makes this safe to
-- apply exactly once; if this somehow ran a second time, a loud "duplicate
-- column name" error is the CORRECT behavior, not something to hide.
ALTER TABLE panchakarma_resources ADD COLUMN clinic_id TEXT NOT NULL DEFAULT 'default_clinic';

CREATE INDEX IF NOT EXISTS idx_panchakarma_resources_clinic_id ON panchakarma_resources(clinic_id);
