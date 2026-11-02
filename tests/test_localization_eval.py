from pathlib import Path
import tempfile
import unittest

from dfx_agent.localization_eval import evaluate
from dfx_agent.workspace import save_json


class LocalizationEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.run = self.root/'run'; (self.run/'baseline').mkdir(parents=True)
        self.raw = b'first\nsecond\nthird\n'; (self.run/'baseline/a.ets').write_bytes(self.raw)
        save_json(self.run/'state.json', {'run_id':'run', 'case_id':'demo', 'snapshot_id':'snapshot',
                                        'model_kind':'offline_fixture','evaluation_scope':'synthetic'})
        save_json(self.run/'snapshot.json', {'snapshot_id':'snapshot','files':{'a.ets':len(self.raw)}})
        self.label = {'schema_version':2, 'source_version':'test-source-v1','case_id':'demo','snapshot_id':'snapshot','label_version':'fixture-labels-v1','locations':{
            'root_cause':{'status':'CONFIRMED','reviewer':'test fixture','provenance':['synthetic oracle'],
                          'points':[{'path':'a.ets','line':2}]},
            'crash_location':{'status':'OBSERVED'},'edit_location':{'status':'UNLABELED'}}}
        self.path = self.root/'labels.json'; self.save()

    def save(self): save_json(self.path,self.label)
    def prediction(self,path='a.ets',line=2):
        save_json(self.run/'attempts/001/proposal.json',{'localization':{'root_cause':{'path':path,'line':line}}})
    def score(self): return evaluate(self.run,self.path)

    def test_exact_and_file_only_are_separate(self):
        self.prediction(); self.assertTrue(self.score()['locations']['root_cause']['exact_line_hit'])
        self.prediction(line=3); row=self.score()['locations']['root_cause']
        self.assertTrue(row['file_hit']); self.assertFalse(row['exact_line_hit'])
        self.prediction(path='wrong.ets'); self.assertFalse(self.score()['locations']['root_cause']['file_hit'])

    def test_provisional_root_never_inherits_crash_prediction(self):
        self.label['locations']['root_cause']['status']='CANDIDATE'; self.save(); self.prediction()
        result=self.score()
        self.assertIsNone(result['locations']['root_cause']['exact_line_hit'])
        self.assertIsNone(result['aggregate_accuracy']); self.assertFalse(result['official_score'])
        self.assertEqual(result['model_kind'],'offline_fixture')

    def test_missing_and_malformed_prediction_are_not_scored(self):
        self.assertEqual(self.score()['locations']['root_cause']['status'],'NOT_PREDICTED')
        self.prediction(line=True)
        self.assertEqual(self.score()['locations']['root_cause']['status'],'INVALID_PREDICTION')

    def test_different_case_version_rejected(self):
        self.label['snapshot_id']='other'; self.save()
        with self.assertRaises(ValueError): self.score()

    def test_confirmed_label_requires_review_and_valid_original_line(self):
        self.label['locations']['root_cause']['reviewer']=''; self.save()
        with self.assertRaises(ValueError): self.score()
        self.label['locations']['root_cause']['reviewer']='test'
        self.label['locations']['root_cause']['points'][0]['line']=40; self.save()
        with self.assertRaises(ValueError): self.score()

    def test_attempt_is_explicit_no_best_of_selection(self):
        self.prediction()
        self.assertEqual(evaluate(self.run,self.path,2)['locations']['root_cause']['status'],'NOT_PREDICTED')
        with self.assertRaises(ValueError): evaluate(self.run,self.path,0)
