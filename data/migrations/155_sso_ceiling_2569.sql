-- 155_sso_ceiling_2569.sql
--
-- ประกันสังคม (มาตรา 33) maximum wage base: 15,000 → 17,500.
--
-- WHY: the ceiling is set by กฎกระทรวง, not by the พ.ร.บ.ประกันสังคม itself
-- (มาตรา 46 delegates it), so it moves without the Act changing. A regulation
-- published in ราชกิจจานุเบกษา on 12 ธ.ค. 2568 raised it to 17,500 with effect
-- from 1 ม.ค. 2569 — maximum contribution 750 → 875 at the unchanged 5% rate.
--
-- Sendy seeded 15,000 in mig 054 and never revisited it, so since Jan 2026 it
-- has under-deducted and under-remitted for anyone earning above 15,000.
--
-- ⚠ ALREADY SCHEDULED — this will need doing again:
--     2572–2574  ceiling 20,000  (max 1,000)
--     2575 →     ceiling 23,000  (max 1,150)
--   Write a new migration each time; do not edit this one.
--
-- BLAST RADIUS: none on today's data. Only two employees are sso_enrolled
-- (หลุย 15,000 and บอล 13,000) and neither is above the OLD ceiling, so no
-- current contribution changes. Pinned by
-- tests/test_mig155_sso_ceiling_2569.py::test_no_currently_enrolled_employee_is_affected,
-- which goes red the moment a raise pushes an enrolled employee past 15,000.
--
-- Finalized payroll runs are NOT recomputed (generate_run returns early on a
-- finalized run), so no issued payslip is rewritten by this.
--
-- Background: ~/FlawlessOS/wiki/legal/thai-social-security-contributions.md
--
-- Apply: the runner applies it (database.py::run_pending_migrations).

UPDATE hr_config
   SET value = '17500',
       note  = 'ฐานค่าจ้างขั้นสูงสำหรับคำนวณประกันสังคม (บาท/เดือน) — 17,500 ตั้งแต่ 1 ม.ค. 2569 (กฎกระทรวง, ราชกิจจานุเบกษา 12 ธ.ค. 2568); ขั้นถัดไป 20,000 ปี 2572, 23,000 ปี 2575'
 WHERE key = 'sso_max_base';
