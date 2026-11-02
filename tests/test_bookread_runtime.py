import copy
import unittest
import tempfile
from pathlib import Path

from dfx_agent.bookread_runtime import assert_books, merge_rows, more_button, page_rows, consent
from dfx_agent.harmony_runtime import Blocked,CheckFailed
from dfx_agent.workspace import snapshot
from dfx_agent.outbound import validation_feedback
from dfx_agent.context_review import verify_approval


def node(kind,text='',children=(),**attrs):
    return {'attributes':{'type':kind,'text':text,'visible':'true','bounds':'[0,0][500,900]',**attrs},'children':list(children)}


def screen(rows):
    return node('Column',children=[node('Text','栏目'),node('List',children=[
        node('ListItem',children=[node('Text','免费'),
            node('Row',children=[node('Text',r['name'],bounds=f'[150,{110+i*150}][400,{140+i*150}]'),node('Row',children=[node('Text','9.0')])]),
            node('Text','简介'),node('Text','作者：'+r['author'],bounds=f'[150,{200+i*150}][400,{230+i*150}]')],bounds=f'[0,{100+i*150}][500,{250+i*150}]')
        for i,r in enumerate(rows)],bounds='[0,100][500,850]')])


class BookReadTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'name':str(i),'author':'author'+str(i)} for i in range(6)]
        self.sample={'section':'栏目','expected_books':self.rows}

    def test_consent_button_is_reported_as_user_action_not_navigation_timeout(self):
        for kind in ('Button','Text'):
            with self.subTest(kind=kind),self.assertRaisesRegex(Blocked,'User agreement'):
                consent(node('Column',children=[node(kind,'同意')]))
        consent(node('Column',children=[node('Button','同意',visible='false')]))

    def test_full_sequence_passes_but_empty_truncated_extra_duplicate_wrong_author_stale_fail(self):
        self.assertEqual(assert_books(self.rows,self.sample)['status'],'PASS')
        wrong=copy.deepcopy(self.rows);wrong[1]['author']='wrong'
        for rows in ([],self.rows[:3],self.rows[:-1],self.rows+[self.rows[0]],self.rows[:2]+self.rows[1:],wrong,[{'name':'old','author':'old'}]):
            with self.subTest(rows=rows),self.assertRaisesRegex(CheckFailed,'LIST_CONTENT_MISMATCH'):
                assert_books(rows,self.sample)

    def test_order_failure_is_distinct(self):
        with self.assertRaisesRegex(CheckFailed,'LIST_ORDER_MISMATCH'):
            assert_books(list(reversed(self.rows)),self.sample)

    def test_overlapping_viewports_preserve_full_sequence_and_do_not_set_deduplicate(self):
        merged=merge_rows(self.rows[:3],self.rows[2:5])
        self.assertEqual(merge_rows(merged,self.rows[4:]),self.rows)
        merged=merge_rows(self.rows[:3],[self.rows[2],self.rows[2],self.rows[3]])
        self.assertEqual(merged,self.rows[:3]+[self.rows[2],self.rows[3]])
        with self.assertRaises(CheckFailed):assert_books(merged,self.sample)
        with self.assertRaises(Blocked):merge_rows(self.rows[:2],self.rows[3:])
        with self.assertRaises(Blocked):merge_rows([self.rows[0]]*2,[self.rows[0]]*2)

    def test_ui_rows_pair_titles_with_authors_and_exclude_only_clipped_rows(self):
        tree=screen(self.rows)
        _,rows=page_rows(tree,'栏目')
        self.assertEqual([r for _,_,r in rows],self.rows[:5])
        tree['children'][1]['children'][1]['children'].pop()
        with self.assertRaises(Blocked):page_rows(tree,'栏目')

    def test_wrong_title_and_ambiguous_list_do_not_pass(self):
        tree=screen(self.rows[:2])
        with self.assertRaises(CheckFailed):page_rows(tree,'other')
        tree['children'].append(node('List'))
        with self.assertRaises(Blocked):page_rows(tree,'栏目')

    def test_viewport_clamped_partial_row_is_deferred_but_interior_missing_author_blocks(self):
        tree=screen(self.rows[:3])
        last=tree['children'][1]['children'][-1]
        last['attributes']['bounds']='[0,700][500,850]'
        last['children'].pop()
        _,rows=page_rows(tree,'栏目')
        self.assertEqual([r for _,_,r in rows],self.rows[:2])
        last['attributes']['bounds']='[0,400][500,550]'
        with self.assertRaises(Blocked):page_rows(tree,'栏目')

    def test_more_is_selected_in_its_section_not_arbitrary_first_control(self):
        other=node('Text','更多');wanted=node('Text','更多')
        tree=node('Column',children=[node('Row',children=[node('Text','其他'),other]),node('Row',children=[node('Text','目标'),wanted])])
        self.assertIs(more_button(tree,'目标'),wanted)
        self.assertIsNone(more_button(tree,'缺失'))
        clipped=node('Column',children=[node('Text','目标'),node('Row',children=[node('Text','其他'),other])])
        self.assertIsNone(more_button(clipped,'目标'))

    def test_snapshot_keeps_nested_screenshot_source_but_excludes_root_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'source';root.mkdir()
            source=root/'components/share/src/screenshot/Screenshot.ets'
            source.parent.mkdir(parents=True);source.write_text('export const value=1;',encoding='utf-8')
            artifact=root/'screenshot/image.png';artifact.parent.mkdir();artifact.write_bytes(b'png')
            dest=Path(folder)/'copy';snapshot(root,dest)
            self.assertEqual((dest/source.relative_to(root)).read_bytes(),source.read_bytes())
            self.assertFalse((dest/'screenshot').exists())

    def test_list_feedback_is_opt_in_and_never_sends_records_or_counts(self):
        item={'stage':'functional','result':{'status':'FAIL','details':{'reason':'LIST_ORDER_MISMATCH','expected':['PRIVATE_BOOK'],'observed':['SECRET_AUTHOR'],'count':22}}}
        self.assertEqual(validation_feedback(item)['result']['failure_kind'],'VALIDATION_NOT_PASSED')
        result=validation_feedback(item,include_list_diagnostics=True)
        self.assertEqual(result['result'],{'status':'FAIL','failure_kind':'LIST_ORDER_MISMATCH'})
        item['result']['details']['reason']='LIST_ORDER_MISMATCH SECRET'
        self.assertEqual(validation_feedback(item,include_list_diagnostics=True)['result']['failure_kind'],'VALIDATION_NOT_PASSED')
        config={'source_version':'v1','visible_files':[],'editable_files':[],'logs':[], 'include_list_diagnostics':True,
                'approved_context':{'source_version':'v1','visible_files':[],'editable_files':[],'log_names':[]}}
        with self.assertRaises(ValueError):verify_approval(config)
        config['approved_context']['include_list_diagnostics']=True
        verify_approval(config)


if __name__=='__main__':unittest.main()
