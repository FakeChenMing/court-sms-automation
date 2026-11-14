import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from deadline_math import calculate,working
from datetime import date
class Dates(unittest.TestCase):
 def test_2027_new_year(self):
  x=calculate({'anchor_date':'2026-12-17','amount':15,'unit':'calendar_days','adjustment':'next_working'})
  self.assertEqual(x['unadjusted'],'2027-01-01');self.assertEqual(x['date'],'2027-01-04')
 def test_same_day_reference(self):self.assertEqual(calculate({'anchor_date':'2026-12-17','amount':0,'unit':'calendar_days'})['date'],'2026-12-17')
 def test_working_weekend(self):self.assertTrue(working(date(2027,2,14)))
 def test_month_end(self):self.assertEqual(calculate({'anchor_date':'2027-01-31','amount':1,'unit':'months'})['date'],'2027-02-28')
 def test_unverified_year(self):
  with self.assertRaises(ValueError):calculate({'anchor_date':'2027-12-30','amount':5,'unit':'calendar_days'})
 def test_backward_workdays(self):self.assertEqual(calculate({'anchor_date':'2027-02-16','amount':2,'unit':'working_days','direction':-1})['date'],'2027-02-14')
if __name__=='__main__':unittest.main()
