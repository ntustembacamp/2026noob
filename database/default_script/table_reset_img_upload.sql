-- 在VSCODE的mySQL套件中直接建立
CREATE TABLE img_upload (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '主鍵',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '資料建立時間',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '資料最後更新時間',
    origin_full_path NVARCHAR(400) COMMENT '原檔檔案路徑',
    thumbs_full_path NVARCHAR(400) COMMENT '縮圖檔案路徑',
    human_activity_date NVARCHAR(64) COMMENT '人工活動日期',
    human_activity_name NVARCHAR(255) COMMENT '人工活動名稱',
    human_laptop_number NVARCHAR(64) COMMENT '人工筆電編號',
    human_photographer NVARCHAR(255) COMMENT '人工攝影師',
    human_photo_time DATETIME COMMENT '人工拍攝年月日時分秒毫秒',
    img_score FLOAT COMMENT 'pyiqa圖片分數',
    UNIQUE INDEX idx_unique (origin_full_path, thumbs_full_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
