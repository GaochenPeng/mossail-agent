import json
import pytest
from agents.models.interface import Model
from openai.types.responses import Response, ResponseCompletedEvent, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from open_protocol import AgentInput
from agent_runtime.runtime import AgentRuntime, AgentSpec
from agent_runtime.tools import calculate


class ScriptedModel(Model):
    """只用于 SDK 适配回归；不作为真实模型验证。"""
    def __init__(self):
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        raise NotImplementedError

    async def stream_response(self, *args, **kwargs):
        self.inputs.append(kwargs.get('input', args[1] if len(args) > 1 else None))
        turn = len(self.inputs)
        if turn == 1:
            output = [ResponseFunctionToolCall(id='fc-1', call_id='call-1', name='calculate',
                        arguments=json.dumps({'expression': '(17+25)*3'}), type='function_call', status='completed')]
        else:
            output = [ResponseOutputMessage(id='msg-1', role='assistant', type='message', status='completed',
                content=[ResponseOutputText(type='output_text', text='工具结果是126。', annotations=[], logprobs=[])])]
        response = Response(id=f'response-{turn}', created_at=0, model='scripted', object='response',
                            output=output, parallel_tool_calls=False, tool_choice='auto', tools=[], status='completed')
        yield ResponseCompletedEvent(type='response.completed', response=response, sequence_number=turn)


@pytest.mark.asyncio
async def test_sdk_runs_real_function_tool_and_formats_protocol():
    model = ScriptedModel()
    runtime = AgentRuntime(model, AgentSpec('test', 'Use the calculator.', [calculate]))
    updates = [item async for item in runtime.stream(AgentInput(thread_id='t', task_id='r', user_prompt='calculate'))]
    assert len(model.inputs) == 2
    assert '126.0' in json.dumps(model.inputs[1])
    assert any(u.event_v2.op == 'add' and u.event_v2.msg_id == 'call-1' for u in updates)
    tool_start = next(u.event_v2.value for u in updates if u.event_v2.msg_id == 'call-1' and u.event_v2.op == 'add')
    assert 'output' in tool_start.meta_data  # 后续 replace 的路径必须先存在。
    assert any(p.path == '/meta_data/output' and p.value == '126.0' for u in updates for p in u.event_v2.patches or [])
    assert updates[-1].event_v2.task_status == 'complete'
    assert all((u.event_v2.thread_id, u.event_v2.task_id) == ('t', 'r') for u in updates)
    user = updates[0].event_v2.value
    assert user.meta_data['execution']['agent'] == 'test'
