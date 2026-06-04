SELECT 
  file_name, 
  JSON_ARRAYAGG(jt.name) AS name_array,
  file_name != JSON_UNQUOTE(JSON_ARRAYAGG(jt.name)) AS res_check
FROM 
  reco_result,
  JSON_TABLE(
    reco_res, '$[*]'
    COLUMNS (name VARCHAR(255) PATH '$.name')
  ) AS jt
WHERE jt.name IS NOT NULL
GROUP BY file_name LIMIT 200