
-- 在VSCODE的mySQL套件中直接建立
CREATE TABLE base (
    id INT NOT NULL PRIMARY KEY AUTO_INCREMENT COMMENT '主鍵',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '建立時間',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最後更新時間',
    dept VARCHAR(64) NOT NULL COMMENT '部門',
    year INT NOT NULL COMMENT '年度',
    team VARCHAR(64) COMMENT '團隊',
    name VARCHAR(255) COMMENT '姓名',
    phash VARCHAR(255) COMMENT '感知雜湊值',
    file_path VARCHAR(255) COMMENT '檔案路徑',
    file_name VARCHAR(255) COMMENT '檔案名稱',
    UNIQUE INDEX idx_unique (dept, year, team, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;