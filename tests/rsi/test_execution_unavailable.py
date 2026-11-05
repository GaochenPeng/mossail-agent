"""清理旧 Backend 后，不允许未适配评测读凭证、启动进程或产出伪通过。"""
import subprocess
import sys

import pytest

from rsi.execution import ExecutionUnavailable
from rsi.experiment import Experiment
from scripts import rsi as cli


@pytest.mark.parametrize('action', ['evaluate', 'run'])
def test_cli_rejects_unadapted_execution_before_configuration_or_artifacts(tmp_path, monkeypatch, action):
    output = tmp_path / 'must-not-be-created'
    arguments = ['rsi.py', action, '--output', str(output)]
    if action == 'evaluate':
        arguments += ['--experiment', str(tmp_path / 'missing-experiment')]
    else:
        arguments += ['--proposal', str(tmp_path / 'missing-proposal.json')]
    monkeypatch.setattr(sys, 'argv', arguments)

    def forbidden(*args, **kwargs):
        pytest.fail('未适配执行不应读取配置、创建实验或启动进程')

    monkeypatch.setattr(cli, 'configuration', forbidden)
    monkeypatch.setattr(Experiment, 'prepare', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(ExecutionUnavailable, match='PG/Redis'):
        cli.main()
    assert not output.exists()


def test_direct_experiment_evaluation_cannot_bypass_execution_boundary(tmp_path):
    experiment = Experiment(tmp_path / 'missing-experiment')
    with pytest.raises(ExecutionUnavailable, match='尚未适配'):
        experiment.evaluate({})
    assert not experiment.directory.exists()
