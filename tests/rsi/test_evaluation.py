import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('evaluation', Path(__file__).resolve().parents[2]/'scripts/evaluate.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_answer_number_cannot_substitute_for_a_correct_tool_result():
    task = {'status':'complete','snapshots':{
        'a':{'type':'markdown','sender':'assistant','status':'success','meta_data':{'content':'126'}},
        't':{'type':'tool_call','sender':'assistant','status':'success','meta_data':{'name':'calculate','output':'125'}}}}
    checks = evaluation.judge(task, {'tool_names':['calculate'],'tool_number':'126','answer_patterns':['126']})
    assert checks['answer:126']
    assert not checks['exact_tool_number']
    assert not all(checks.values())


def test_a_valid_looking_citation_without_read_evidence_fails():
    task = {'status':'complete','snapshots':{
        'a':{'type':'markdown','sender':'assistant','status':'success','meta_data':{
            'content':'3.11 [cit-1](https://peps.python.org/pep-0654/)',
            'source':[{'id':'cit-1','url':'https://peps.python.org/pep-0654/','content':'','retrieved_at':None}]}},
        't':{'type':'tool_call','sender':'assistant','status':'success','meta_data':{'name':'web_search','output':'{}'}}}}
    checks = evaluation.judge(task, {'tool_names':['web_search','content_fetch'],
        'source_url':'https://peps.python.org/pep-0654/','evidence_patterns':['3\\.11']})
    assert checks['expected_primary_source']
    assert not checks['citations_resolve_to_read_sources']
    assert not checks['required_tools']
    assert not all(checks.values())
