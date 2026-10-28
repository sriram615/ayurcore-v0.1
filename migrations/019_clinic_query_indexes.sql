-- Migration 019: query indexes for clinic-enriched columns (migration 018).
--
-- Adds indexes on the new filter columns from migration 018 so that
-- clinic-facing queries (payment status, expiry, prakriti) are efficient.
-- All indexes are clinic-scoped (clinic_id first) to support multi-tenant
-- queries without scanning the whole table.

CREATE INDEX IF NOT EXISTS idx_prescriptions_clinic_payment
    ON prescriptions(clinic_id, payment_status);

CREATE INDEX IF NOT EXISTS idx_inventory_clinic_expiry
    ON inventory(clinic_id, expiry_date);

CREATE INDEX IF NOT EXISTS idx_patients_clinic_prakriti
    ON patients(clinic_id, prakriti);

CREATE INDEX IF NOT EXISTS idx_prescriptions_clinic_dispensed
    ON prescriptions(clinic_id, dispensed_at);
