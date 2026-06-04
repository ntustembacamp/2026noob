CREATE TABLE reco_result (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '流水號',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '建立時間',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新時間',
    origin_full_path NVARCHAR(400) COMMENT '原圖完整路徑',
    thumbs_full_path NVARCHAR(400) COMMENT '縮圖完整路徑',
    photo_taken_time DATETIME COMMENT '照片 EXIF 拍攝時間',
    reco_count INT COMMENT '已辨識人臉數',
    reco_unknow INT COMMENT '未辨識人臉數',
    reco_res JSON COMMENT '辨識結果 JSON',
    reco_name JSON COMMENT '辨識姓名清單 JSON',
    UNIQUE INDEX idx_unique (origin_full_path, thumbs_full_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
