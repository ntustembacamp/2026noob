SELECT *, JSON_UNQUOTE(JSON_EXTRACT(ai_tag, '$[0]')) 
FROM img_tag
-- WHERE ai_tag LIKE '%午餐%' 
LIMIT 100