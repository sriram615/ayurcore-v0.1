import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'ayurcore.db')
DEFAULT_CLINIC_ID = "default_clinic"


def seed_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    herbs = [
        (DEFAULT_CLINIC_ID, "Ashwagandha", "Withania somnifera", 5000, "BATCH-ASH-01", 95),
        (DEFAULT_CLINIC_ID, "Brahmi", "Bacopa monnieri", 3500, "BATCH-BRA-02", 92),
        (DEFAULT_CLINIC_ID, "Dashamoola", "Dasamula (10 Roots)", 8000, "BATCH-DASH-01", 88),
        (DEFAULT_CLINIC_ID, "Guggulu", "Commiphora wightii", 2000, "BATCH-GUG-05", 96),
        (DEFAULT_CLINIC_ID, "Shatavari", "Asparagus racemosus", 6000, "BATCH-SHA-03", 94),
        (DEFAULT_CLINIC_ID, "Triphala", "Emblica officinalis, Terminalia chebula, Terminalia bellirica", 10000, "BATCH-TRI-04", 90),
        (DEFAULT_CLINIC_ID, "Arjuna", "Terminalia arjuna", 4000, "BATCH-ARJ-02", 91),
        (DEFAULT_CLINIC_ID, "Haridra", "Curcuma longa", 12000, "BATCH-HAR-01", 98),
        # Added when the formulary grew from 8 to 20 herbs (Sep 2026).
        # IMPORTANT: every number below (stock_grams, batch_code,
        # potency_score) is placeholder seed data for development and
        # testing, exactly like the original 8 rows above always were --
        # none of this has ever been the clinic's real, physically counted
        # stock. Before any of these 12 herbs are used with a real
        # patient, the doctor/clinic needs to replace these with actual
        # counted stock. Nothing about this system enforces that today --
        # it's a manual step, called out here on purpose.
        (DEFAULT_CLINIC_ID, "Neem", "Azadirachta indica", 4000, "BATCH-NEE-01", 90),
        (DEFAULT_CLINIC_ID, "Yashtimadhu", "Glycyrrhiza glabra", 3000, "BATCH-YAS-01", 93),
        (DEFAULT_CLINIC_ID, "Punarnava", "Boerhavia diffusa", 3500, "BATCH-PUN-01", 89),
        (DEFAULT_CLINIC_ID, "Bhringraj", "Eclipta alba", 2500, "BATCH-BHR-01", 91),
        (DEFAULT_CLINIC_ID, "Manjistha", "Rubia cordifolia", 2000, "BATCH-MAN-01", 90),
        (DEFAULT_CLINIC_ID, "Vasaka", "Justicia adhatoda", 3000, "BATCH-VAS-01", 92),
        (DEFAULT_CLINIC_ID, "Shankhpushpi", "Convolvulus pluricaulis", 1500, "BATCH-SHP-01", 87),
        (DEFAULT_CLINIC_ID, "Kutki", "Picrorhiza kurroa", 1000, "BATCH-KUT-01", 85),
        (DEFAULT_CLINIC_ID, "Pippali", "Piper longum", 2500, "BATCH-PIP-01", 94),
        (DEFAULT_CLINIC_ID, "Haritaki", "Terminalia chebula", 5000, "BATCH-HAT-01", 93),
        (DEFAULT_CLINIC_ID, "Amalaki", "Phyllanthus emblica", 6000, "BATCH-AML-01", 96),
        (DEFAULT_CLINIC_ID, "Bibhitaki", "Terminalia bellirica", 4500, "BATCH-BIB-01", 92),
    ]

    # Clear the table first to keep this idempotent when run multiple times.
    cursor.execute('DELETE FROM inventory')

    cursor.executemany('''
        INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', herbs)

    conn.commit()
    conn.close()
    print("Database seeded successfully with Ayurvedic herbs.")


if __name__ == "__main__":
    seed_db()
