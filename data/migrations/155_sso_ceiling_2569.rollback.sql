-- 155_sso_ceiling_2569.rollback.sql
--
-- Restore the pre-2569 ประกันสังคม ceiling (15,000) and mig 054's original note.
--
-- ⚠ Rolling back puts Sendy back to under-remitting for anyone earning above
-- 15,000, which is the legally wrong figure from 1 ม.ค. 2569 onward. Only use
-- this to undo a bad deploy, not as a way to keep the old number.
--
-- Remember to DELETE the row from applied_migrations by hand as well — the
-- runner is filename-keyed and will not re-apply otherwise.

UPDATE hr_config
   SET value = '15000',
       note  = 'ฐานค่าจ้างขั้นสูงสำหรับคำนวณประกันสังคม (บาท/เดือน)'
 WHERE key = 'sso_max_base';
