-- =============================================================
-- Penelope — Schema MariaDB (Proxmox)
-- =============================================================
-- Eseguito su server Uninet/Proxmox (Celeron, 2GB RAM).
-- Database: penelope

CREATE DATABASE IF NOT EXISTS penelope
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE penelope;

-- ─── NODI ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    id          VARCHAR(64) PRIMARY KEY COMMENT 'UUID v4',
    type        VARCHAR(50) NOT NULL DEFAULT 'File'
                COMMENT 'File, Directory, Project, Person, Location, Event, Object',
    label       VARCHAR(255) DEFAULT NULL COMMENT 'Nome leggibile',
    category    VARCHAR(20) DEFAULT NULL COMMENT 'image|video|document|other (per File)',
    parent_id   VARCHAR(64) DEFAULT NULL COMMENT 'Gerarchia filesystem: Directory/Project genitore',
    metadata    JSON DEFAULT NULL COMMENT 'Attributi variabili per tipo',
    analyzed    JSON DEFAULT NULL COMMENT 'Stato analisi: yolo/face/ner/meta',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_label (label),
    INDEX idx_category (category),
    INDEX idx_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─── ARCHI (relazioni) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    source_id   VARCHAR(64) NOT NULL,
    target_id   VARCHAR(64) NOT NULL,
    relation    VARCHAR(50) NOT NULL COMMENT 'MEMBER_OF, APPEARS_IN, MENTIONS, CREATED_AT, SIMILAR_TO',
    weight      FLOAT DEFAULT 1.0,
    metadata    JSON DEFAULT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE,
    INDEX idx_relation (relation),
    INDEX idx_source (source_id),
    INDEX idx_target (target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─── OGGETTI (cache YOLO detections) ────────────────────────────────
CREATE TABLE IF NOT EXISTS objects (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    label       VARCHAR(100) NOT NULL COMMENT 'Nome oggetto (es. person, car, dog)',
    coco_class_id INT DEFAULT NULL COMMENT 'COCO class ID per YOLO',
    category    VARCHAR(50) DEFAULT NULL COMMENT 'person, vehicle, animal, ...',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_label (label),
    INDEX idx_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─── NODE OBJECTS (many-to-many: file -> oggetto) ──────────────────
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─── FILE REGISTRY (path fisici su dispositivi) ────────────────────
CREATE TABLE IF NOT EXISTS file_registry (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    node_id         VARCHAR(64) NOT NULL,
    device          VARCHAR(50) NOT NULL COMMENT 'laptop-main, headless, hdd-ext, smartphone',
    path            TEXT NOT NULL,
    size_bytes      BIGINT DEFAULT NULL,
    sha256          CHAR(64) DEFAULT NULL,
    mime_type       VARCHAR(100) DEFAULT NULL,
    last_seen       DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    INDEX idx_device (device),
    INDEX idx_sha256 (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─── CODA DI ELABORAZIONE (lazy processing) ────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_queue (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    node_id     VARCHAR(64) NOT NULL,
    status      ENUM('pending','processing','done','failed') DEFAULT 'pending',
    priority    INT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    error_msg   TEXT DEFAULT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    INDEX idx_status (status),
    INDEX idx_priority (priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
