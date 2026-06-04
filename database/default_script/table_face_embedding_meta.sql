CREATE TABLE IF NOT EXISTS face_embedding_meta (
    id INT AUTO_INCREMENT PRIMARY KEY,
    base_id INT NOT NULL,
    user_name VARCHAR(255) NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    phash VARCHAR(64) DEFAULT '',
    model_name VARCHAR(64) NOT NULL,
    embedding_exists TINYINT(1) NOT NULL DEFAULT 0,
    face_count INT NOT NULL DEFAULT 0,
    status VARCHAR(64) NOT NULL DEFAULT 'pending',
    error_message TEXT NULL,
    embedding_update_time DATETIME NULL,
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_face_embedding_meta_base_model (base_id, model_name),
    KEY idx_face_embedding_meta_user_name (user_name),
    KEY idx_face_embedding_meta_status (status)
);
