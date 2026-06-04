-- 在VSCODE的mySQL套件中直接建立
CREATE TABLE img_tag (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '主鍵',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '資料建立時間',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '資料最後更新時間',
    origin_full_path NVARCHAR(400) COMMENT '原檔檔案路徑',
    thumbs_full_path NVARCHAR(400) COMMENT '縮圖檔案路徑',
    ai_activity_date NVARCHAR(64) COMMENT 'AI活動日期',
    ai_activity_time NVARCHAR(64) COMMENT 'AI活動時間',
    ai_activity_name NVARCHAR(64) COMMENT 'AI活動名稱',
    ai_tag JSON COMMENT 'AI Tag',
    UNIQUE INDEX idx_unique (origin_full_path, thumbs_full_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
