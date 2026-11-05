"""模型只接收需求和允许修改的源码；封存检查、结果与密钥不进入提示词。"""
import json
from pathlib import Path

from .workspace import digest, validate_changes, write_json


def generate(plan: dict, baseline: Path, configuration: dict, output: Path) -> dict:
    from openai import OpenAI
    files = {name:(baseline/name).read_text() for name in plan['allowed_files']}
    request = {'objective':plan['objective'],'development_note':plan['development_note'],'files':files}
    schema = {'type':'object','additionalProperties':False,'required':['rationale','changes'],
        'properties':{'rationale':{'type':'string'},'changes':{'type':'array','minItems':1,'items':{
            'type':'object','additionalProperties':False,'required':['path','content'],
            'properties':{'path':{'type':'string','enum':plan['allowed_files']},'content':{'type':'string'}}}}}}
    write_json(output/'generation-input.json',request)
    with OpenAI(api_key=configuration['OPENAI_API_KEY'],base_url=configuration.get('OPENAI_BASE_URL') or None,
                timeout=120,max_retries=1) as client:
        response = client.responses.create(model=configuration['AGENT_MODEL'],max_output_tokens=12000,
            instructions='你是代码改进助手。只修改给定源码，返回完整文件内容和简洁修改理由。'
                '保持接口和主流程清楚，优先最小修复，不改测试、验收器、接受规则或文件范围。不要包含推理过程。',
            input=json.dumps(request,ensure_ascii=False),
            text={'format':{'type':'json_schema','name':'code_candidate','strict':True,'schema':schema}})
    if response.status!='completed': raise RuntimeError('候选生成未正常结束')
    proposal = json.loads(response.output_text)
    validate_changes(proposal,plan['allowed_files'])
    write_json(output/'generation.json',{'model_requested':configuration['AGENT_MODEL'],'model_returned':response.model,
        'response_id':response.id,'usage':response.usage.model_dump() if response.usage else None,
        'request_sha256':digest(request),'proposal_sha256':digest(proposal)})
    write_json(output/'proposal.json',proposal)
    return proposal
