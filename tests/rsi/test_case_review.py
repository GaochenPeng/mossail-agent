from copy import deepcopy

import pytest

from scripts.export_case import build_case
from scripts.review_case import reviewed_case


def material():
    tasks=[{'id':'t','thread_id':'thread','agent':'assistant','status':'complete','error':None,'question':'calculate','snapshots':{
        'tool':{'type':'tool_call','sender':'assistant','status':'success','meta_data':{'name':'calculate','output':'42'}},
        'answer':{'type':'markdown','sender':'assistant','status':'success','meta_data':{'content':'42'}}}}]
    case=build_case(tasks,'synthetic')
    criteria={'reference':{'basis':'6*7 equals 42'},'steps':[{'expect':{'tool_names':['calculate'],'tool_number':'42','answer_patterns':['42']}}]}
    return case,tasks,criteria


def test_case_criteria_are_bound_without_inventing_human_feedback():
    case,tasks,criteria=material()
    reviewed,suite=reviewed_case(case,tasks,criteria,kind='automated',rating='pass',reviewer='fixed-check',notes='deterministic')
    assert reviewed['review']['kind']=='automated'
    assert suite['cases'][0]['steps'][0]['question']=='calculate'
    assert suite['cases'][0]['source_case']['raw_tasks_sha256']==case['raw_tasks_sha256']
    assert case['review']['rating']=='unreviewed'
    assert reviewed['publication_status']=='local_draft'


def test_false_automated_pass_or_modified_execution_material_is_rejected():
    case,tasks,criteria=material()
    wrong=deepcopy(criteria);wrong['steps'][0]['expect']['tool_number']='43'
    with pytest.raises(ValueError,match='不一致'):
        reviewed_case(case,tasks,wrong,kind='automated',rating='pass',reviewer='test',notes='test')
    altered=deepcopy(case);altered['turns'][0]['input']='different question'
    with pytest.raises(ValueError,match='不一致'):
        reviewed_case(altered,tasks,criteria,kind='human',rating='pass',reviewer='test',notes='test')
