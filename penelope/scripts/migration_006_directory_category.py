"""
Migration 006: Aggiunge supporto a Directory, Object, categoria file.

Modifiche:
1. ALTER nodes.type ENUM → aggiunge 'Directory', 'Object'
2. Aggiunge colonna nodes.category = image|video|document|other
3. Aggiunge colonna nodes.parent_id per gerarchia (parent Directory/Project)
4. Aggiunge colonna nodes.analyzed_flag per tracking analisi
5. Crea tabella objects per YOLO detection cache (opzionale)
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from penelope.db.mariadb_store import MariaDBStore
from penelope.config import settings

logger = logging.getLogger("migration_006")


def migrate():
    db = MariaDBStore()
    conn = db.connect()

    logger.info("Migration 006: Directory, Object, category, parent_id...")

    with conn.cursor() as cur:
        # 1. Aggiorna ENUM esistente per includere Directory e Object
        #    (MySQL/MariaDB: MODIFY COLUMN con nuovo ENUM)
        cur.execute("""
            SELECT COLUMN_TYPE FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'nodes'
              AND COLUMN_NAME = 'type'
        """)
        current = cur.fetchone()
        # Estrai il current ENUM
        if current:
            cur.execute("""
                ALTER TABLE nodes
                MODIFY COLUMN type VARCHAR(50) NOT NULL DEFAULT 'File'
            """)
            logger.info("  type: ENUM -> VARCHAR(50) per flessibilità futura")

        # 2. Aggiungi colonna category
        try:
            cur.execute("""
                ALTER TABLE nodes
                ADD COLUMN category VARCHAR(20) DEFAULT NULL
                COMMENT 'image|video|document|other' AFTER type
            """)
            logger.info("  Aggiunta colonna category")
        except Exception as e:
            if "Duplicate column" in str(e):
                logger.info("  colonna category già presente")
            else:
                raise

        # 3. Aggiungi colonna parent_id per gerarchia
        try:
            cur.execute("""
                ALTER TABLE nodes
                ADD COLUMN parent_id VARCHAR(64) DEFAULT NULL AFTER category,
                ADD INDEX idx_parent (parent_id)
            """)
            logger.info("  Aggiunta colonna parent_id")
        except Exception as e:
            if "Duplicate column" in str(e):
                logger.info("  colonna parent_id già presente")
            else:
                raise

        # 4. Aggiungi colonna analyzed (flag JSON per tracking analisi)
        try:
            cur.execute("""
                ALTER TABLE nodes
                ADD COLUMN analyzed JSON DEFAULT NULL
                COMMENT '{"yolo":"done|pending|error","face":"done|pending|error","ner":"done|pending|error","meta":"done|pending|error"}'
                AFTER metadata
            """)
            logger.info("  Aggiunta colonna analyzed")
        except Exception as e:
            if "Duplicate column" in str(e):
                logger.info("  colonna analyzed già presente")
            else:
                raise

        # 5. Crea tabella objects (cache YOLO detections)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                label       VARCHAR(100) NOT NULL COMMENT 'Nome oggetto (es. person, car, dog)',
                coco_class_id INT DEFAULT NULL COMMENT 'COCO class ID per YOLO',
                category    VARCHAR(50) DEFAULT NULL COMMENT 'person, vehicle, animal, ...',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_label (label),
                INDEX idx_category (category)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        logger.info("  Creata tabella objects (cache YOLO)")

        # 6. Crea tabella node_objects (molti-a-molti: file -> object)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS node_objects (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                node_id     VARCHAR(64) NOT NULL,
                object_id   INT NOT NULL,
                confidence  FLOAT DEFAULT 0.0,
                bbox        JSON DEFAULT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
                FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE,
                INDEX idx_node (node_id),
                INDEX idx_object (object_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        logger.info("  Creata tabella node_objects (YOLO detections)")

        conn.commit()
        logger.info("Migration 006 completata con successo.")

    db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    migrate()