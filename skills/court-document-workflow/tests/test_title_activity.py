import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from calendar_content import calendar_title, concise_notes


class TitleActivity(unittest.TestCase):
    case = '（2026）测0113民初99999号'
    parties = [{'role': '原告', 'name': '甲公司'}, {'role': '被告', 'name': '乙公司'}]

    def title(self, **extra):
        return calendar_title({'case_number': self.case, 'category': '鉴定',
                               'subject': '涉案工程', **extra}, self.parties)

    def test_specific_action_survives_generic_category(self):
        self.assertTrue(self.title(action_label='摇号选取鉴定机构').endswith('｜摇号选取鉴定机构'))

    def test_deadline_detail_from_verified_old_generated_title(self):
        title = self.title(category='补证举证', previous_verified_title=self.case + ' 二审新证据举证截止')
        self.assertTrue(title.endswith('｜二审新证据举证截止'))
        self.assertEqual(title.count('截止'), 1)

    def test_subject_only_supplies_activity_not_names_or_location(self):
        title = self.title(category='证据交换', subject='未确认公司证据交换（第二十三法庭）')
        self.assertTrue(title.endswith('｜证据交换'))
        self.assertNotIn('未确认公司', title)
        self.assertNotIn('第二十三法庭', title)

    def test_free_form_title_not_used_as_activity(self):
        title = self.title(title=self.case + ' 未确认公司 开庭 带合同')
        self.assertTrue(title.endswith('｜鉴定'))
        self.assertNotIn('未确认公司', title)
        self.assertNotIn('带合同', title)

    def test_action_label_cannot_reintroduce_party_or_title_parts(self):
        for value in ('甲公司开庭', self.case + ' 开庭', '开庭｜带原件'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.title(action_label=value)

    def test_each_verified_side_shortens_only_in_title(self):
        parties=[{'role':'原告','name':'甲公司'},{'role':'原告','name':'甲某'},{'role':'被告','name':'乙公司'},{'role':'被告','name':'乙某'}]
        event={'case_number':self.case,'category':'开庭','subject':'开庭','event_kind':'appointment','source_page':1,'event_key':'fixture','all_day':False}
        self.assertEqual(calendar_title(event,parties),self.case+'｜甲公司等诉乙公司等｜开庭')
        notes=concise_notes(event,parties,'notice',{'notice':{'source_path':'/fixture/开庭传票.pdf'}})
        for party in parties:self.assertIn(party['name'],notes)
        self.assertEqual(len(parties),4)

    def test_one_side_missing_remains_visible_after_abbreviation(self):
        event={'case_number':self.case,'category':'开庭'}
        title=calendar_title(event,[{'role':'原告','name':'甲公司'},{'role':'原告','name':'甲某'}])
        self.assertIn('甲公司等（另一方未确认）',title)


if __name__ == '__main__':
    unittest.main()
