-- Active: 1746843887686@@127.0.0.1@3306@mydatabase_file_id)

CREATE TABLE google_file_id (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '主鍵',
    img_file_name NVARCHAR(400) COMMENT '原檔檔案名',
    img_file_id NVARCHAR(400) COMMENT 'google_file_id',
    UNIQUE INDEX idx_unique (img_file_name, img_file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;