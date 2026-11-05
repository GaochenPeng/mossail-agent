import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('case_export',Path(__file__).resolve().parents[2]/'scripts/export_case.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def task(task_id,status='complete'):
    return {'id':task_id,'thread_id':'thread','agent':'assistant','question':'calculate',
        'status':status,'error':'stopped' if status!='complete' else None,'snapshots':{
            'user':{'sender':'user','type':'markdown','status':'success','meta_data':{'content':'calculate','execution':{'source_sha256':'test-hash'}}},
            'tool':{'sender':'assistant','type':'tool_call','status':'success','meta_data':{'name':'calculate','arguments':'{}','output':'42'}},
            'answer':{'sender':'assistant','type':'markdown','status':'success','meta_data':{'content':'42','source':[]}}}}


def test_case_preserves_multiturn_failure_and_does_not_invent_feedback():
    case = exporter.build_case([task('first'),task('second','abandon')],'synthetic')
    assert len(case['turns'])==2
    assert {'arithmetic','multi_turn','incomplete_or_failed'} <= set(case['tags'])
    assert case['turns'][1]['status']=='abandon'
    assert case['turns'][0]['execution']['source_sha256']=='test-hash'
    assert case['review']['rating']=='unreviewed'
    assert case['evaluation_criteria'] is None
    assert case['publication_status']=='local_draft'
    assert '未提供人工评价' in exporter.render_case(case)
